# Server side of k402: issue offers, verify payments, gate endpoints.
#
#   k402 = K402(address_provider=XpubAddressProvider(xpub),
#               backend=NodeBackend("ws://127.0.0.1:17110"))  # PnnBackend() for dev
#   app = FastAPI()
#   k402.install(app)
#
#   @app.post("/summarize")
#   async def summarize(req: Req, payment=Depends(k402.paid(sompi=1_500_000))):
#       ...
from __future__ import annotations

import time
from typing import Optional

from .addresses import AddressProvider
from .backend import ChainBackend
from .schemes import (PAYMENT_HEADER, SCHEME_BLOCKBOOK, SCHEME_EVM, SCHEME_UTXO,
                      BlockbookOffer, EvmOffer, FacilitatorFee, Offer, ProtocolError,
                      UtxoOffer, new_payment_id, parse_payment_header, payment_required_body)
from .store import MemoryStore, PaymentRecord, PaymentStore


class PaymentRequired(Exception):
    """Raised when a request must (re)pay. Carries the 402 body to send."""

    def __init__(self, offers: list[Offer], reason: str = ""):
        self.body = payment_required_body(offers)
        if reason:
            self.body["reason"] = reason
        super().__init__(reason or "payment required")


# Kaspa's anti-spam rule rejects transactions whose outputs fall below a dust/storage-mass
# floor — in practice a payment output must be >= 0.1 KAS. A kaspa-utxo offer priced below
# this is unpayable (the payer literally can't broadcast it), so we quote the floor instead.
MIN_PAYABLE_SOMPI = 10_000_000  # 0.1 KAS


class K402:
    def __init__(self, address_provider: AddressProvider, backend: ChainBackend,
                 store: Optional[PaymentStore] = None, network: str = "mainnet",
                 quote_ttl: int = 600,
                 facilitator_fee: Optional[FacilitatorFee] = None,
                 extra_offers: Optional[list[Offer]] = None,
                 min_payable_sompi: int = MIN_PAYABLE_SOMPI,
                 coin: Optional[str] = None, decimals: int = 8,
                 capture_baseline: bool = True,
                 evm: Optional[dict] = None):
        self.address_provider = address_provider
        self.backend = backend
        self.store = store or MemoryStore()
        self.network = network
        self.quote_ttl = quote_ttl
        self.facilitator_fee = facilitator_fee
        self.extra_offers = extra_offers or []  # e.g. a SessionOffer
        self.min_payable_sompi = min_payable_sompi
        # coin set -> emit blockbook-utxo offers for that Bitcoin-family chain (Pearl, LTC, DOGE…);
        # coin None -> emit kaspa-utxo offers. Verification is identical either way.
        self.coin = coin
        self.decimals = decimals
        # capture_baseline queries the backend at offer time to snapshot already-received funds.
        # Required for REUSED/static addresses (else standing balance auto-verifies). For
        # FRESH-address-per-payment providers the baseline is always 0, so set False to skip the
        # wasted round-trip and make offer creation instant.
        self.capture_baseline = capture_baseline
        # evm dict -> emit `evm` offers: {chain, chain_id, asset, decimals, token?(ERC-20 contract)}.
        # EVM balance can decrease, so capture_baseline should stay True (delta verification).
        self.evm = evm

    # -------------------------------------------------------------- protocol core
    async def create_offer(self, sompi: int, description: str = "") -> Offer:
        """Create an offer and snapshot the pay_to address's already-received amount as the
        baseline, so verification only counts funds paid AFTER the offer. This is what makes a
        reused address (a cold-wallet address, or Blockbook's monotonic totalReceived) safe:
        its standing balance / history can never auto-satisfy an offer."""
        payment_id = new_payment_id()
        charged = max(int(sompi), self.min_payable_sompi)  # never quote below the payable floor
        pay_to = self.address_provider.next_address(payment_id)
        expires = int(time.time()) + self.quote_ttl
        baseline = await self.backend.address_received_sompi(pay_to) if self.capture_baseline else 0
        if self.evm is not None:
            offer: Offer = EvmOffer(
                chain=self.evm["chain"], chain_id=int(self.evm["chain_id"]), asset=self.evm["asset"],
                amount=str(charged), decimals=int(self.evm.get("decimals", 18)), pay_to=pay_to,
                payment_id=payment_id, expires=expires, token=self.evm.get("token"),
                description=description, facilitator_fee=self.facilitator_fee)
        elif self.coin is not None:
            offer = BlockbookOffer(
                coin=self.coin, network=self.network, amount=str(charged),
                decimals=self.decimals, pay_to=pay_to, payment_id=payment_id,
                expires=expires, description=description, facilitator_fee=self.facilitator_fee)
        else:
            offer = UtxoOffer(
                network=self.network,
                amount_sompi=str(charged),
                pay_to=pay_to,
                payment_id=payment_id,
                expires=expires,
                description=description,
                facilitator_fee=self.facilitator_fee,
            )
        self.store.create(PaymentRecord(
            payment_id=payment_id, address=offer.pay_to,
            amount_sompi=charged, expires=offer.expires, baseline=baseline))
        return offer

    async def _demand(self, sompi: int, description: str, reason: str = "") -> PaymentRequired:
        return PaymentRequired(
            [await self.create_offer(sompi, description), *self.extra_offers], reason)

    async def verify(self, header_value: str, sompi: int,
                     description: str = "") -> PaymentRecord:
        """Verify an X-K402-Payment header; returns the consumed PaymentRecord.
        Raises PaymentRequired (with a fresh offer) on any failure."""
        try:
            scheme, txid, payment_id = parse_payment_header(header_value)
        except ProtocolError as e:
            raise await self._demand(sompi, description, str(e))
        expected_scheme = (SCHEME_EVM if self.evm is not None else
                           SCHEME_BLOCKBOOK if self.coin is not None else SCHEME_UTXO)
        if scheme != expected_scheme:
            raise await self._demand(sompi, description, f"unsupported scheme '{scheme}'")

        rec = self.store.get(payment_id)
        if rec is None:
            raise await self._demand(sompi, description, "unknown payment_id")
        if rec.used:
            raise await self._demand(sompi, description, "payment_id already used")
        if rec.expired:
            raise await self._demand(sompi, description, "quote expired")

        # count only funds received AFTER the offer (delta vs baseline), never standing history
        received = await self.backend.address_received_sompi(rec.address) - rec.baseline
        if received < rec.amount_sompi:
            raise await self._demand(
                sompi, description,
                f"address has received {max(received, 0)} of {rec.amount_sompi} since the offer "
                f"(tx {txid} not confirmed yet? retry shortly)")

        if not self.store.mark_used(payment_id):  # lost the race to a parallel request
            raise await self._demand(sompi, description, "payment_id already used")
        rec.used = True
        rec.meta["txid"] = txid
        return rec

    # -------------------------------------------------------------- FastAPI glue
    def paid(self, sompi: int, description: str = ""):
        """FastAPI dependency: `payment = Depends(k402.paid(sompi=...))`."""
        try:
            from fastapi import Request
        except ImportError as e:
            raise ImportError("k402.paid() needs fastapi: pip install 'k402[server]'") from e

        async def dependency(request) -> PaymentRecord:
            header = request.headers.get(PAYMENT_HEADER)
            if not header:
                raise await self._demand(sompi, description)
            return await self.verify(header, sompi, description)

        # `from __future__ import annotations` stringifies closure annotations,
        # and FastAPI can't resolve "Request" from a function-local import —
        # attach the real class so dependency injection sees it.
        dependency.__annotations__["request"] = Request
        return dependency

    def install(self, app) -> None:
        """Register the 402 exception handler so PaymentRequired renders as a
        protocol-compliant HTTP 402 body (not FastAPI's {'detail': ...})."""
        from fastapi.responses import JSONResponse

        @app.exception_handler(PaymentRequired)
        async def _handler(request, exc: PaymentRequired):
            return JSONResponse(status_code=402, content=exc.body)
