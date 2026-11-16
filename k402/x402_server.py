# k402/x402_server.py — a NATIVE x402 V2 server for Kaspa (not a shim).
#
# Speaks the real x402 V2 wire (PAYMENT-REQUIRED / PAYMENT-SIGNATURE / PAYMENT-RESPONSE, the V2 types
# in k402/x402.py) and reuses k402's on-chain verification engine (per-offer address + baseline +
# address_received). The Kaspa binding is the `exact` scheme, assetTransferMethod "submitted": the
# client broadcasts a Kaspa tx to the per-offer address, then proves it with the txid; verify/settle =
# confirm the address received the exact amount since the offer.
#
# SECURITY: the client's echoed `accepted` is untrusted (x402 trust model). We correlate by
# `payment_id` and check against the SERVER's own record (amount, address, baseline) — never the
# client-supplied amount/payTo.
#
# BINDING KNOBS TO RECONCILE with the emerging Kaspa x402 binding before broadcast to real clients:
# the `network` string, `asset` id, and the exact `payload`/`extra` fields (see k402/x402.py). Two
# divergent Kaspa bindings would re-fragment — align these with the community/foundation binding.

import time
from typing import Optional

from . import x402
from .addresses import AddressProvider
from .backend import ChainBackend
from .schemes import new_payment_id
from .store import MemoryStore, PaymentRecord, PaymentStore


class X402Server:
    def __init__(self, address_provider: AddressProvider, backend: ChainBackend,
                 store: Optional[PaymentStore] = None, *,
                 network: str = x402.NETWORK_MAINNET, asset: str = x402.ASSET_KAS,
                 service_name: str = "", quote_ttl: int = 300, capture_baseline: bool = True,
                 kusd=None, kusd_price_cents: Optional[int] = None):
        self.address_provider = address_provider
        self.backend = backend
        self.store = store or MemoryStore()
        self.network = network
        self.asset = asset
        self.service_name = service_name
        self.quote_ttl = quote_ttl
        self.capture_baseline = capture_baseline
        # kusd (a KusdWallet) set -> every PaymentRequired also accepts kUSD (asset = the authority covenant id,
        # amount in cents, payTo = this provider's key); verify/settle check the holder box on the node through
        # the wallet (DESIGN-kusd-market.md §1)
        self.kusd = kusd
        self.kusd_price_cents = kusd_price_cents

    # ---- build the 402 (PaymentRequired) ------------------------------------------------
    async def demand(self, amount_sompi: int, resource_url: str, description: str = "") -> dict:
        payment_id = new_payment_id()
        pay_to = self.address_provider.next_address(payment_id)
        baseline = await self.backend.address_received_sompi(pay_to) if self.capture_baseline else 0
        self.store.create(PaymentRecord(payment_id=payment_id, address=pay_to,
                                        amount_sompi=int(amount_sompi),
                                        expires=int(time.time()) + self.quote_ttl, baseline=baseline))
        req = x402.requirements(network=self.network, amount_sompi=int(amount_sompi),
                                pay_to=pay_to, payment_id=payment_id,
                                max_timeout_seconds=self.quote_ttl, asset=self.asset)
        accepts = [req]
        if self.kusd is not None and self.kusd_price_cents:
            kid = new_payment_id()
            payee = self.kusd.payee_xonly()
            daa = await self.backend.daa_score() if hasattr(self.backend, "daa_score") else 0
            self.store.create(PaymentRecord(payment_id=kid, address=payee, amount_sompi=int(self.kusd_price_cents),
                                            expires=int(time.time()) + self.quote_ttl, baseline=int(daa), meta={"kusd": True}))
            accepts.append(x402.kusd_requirements(network=self.network, amount_cents=int(self.kusd_price_cents), pay_to_pubkey=payee,
                                                  authority=self.kusd.family().authority, payment_id=kid, max_timeout_seconds=self.quote_ttl))
        resource = x402.resource_info(resource_url, description=description or None,
                                      service_name=self.service_name or None)
        return x402.payment_required(resource=resource, accepts=accepts)

    # ---- shared receipt check (server-truth, client payload untrusted) ------------------
    async def _check(self, payment_id: Optional[str], txid: Optional[str] = None):
        """Returns (record, ok, reason). Reasons use x402 machine codes. A kUSD record (meta.kusd) is checked
        through the wallet: the holder box of exactly the quoted cents, owned by this key, on the node."""
        if not payment_id:
            return None, False, "invalid_payload"
        rec = self.store.get(payment_id)
        if rec is None:
            return None, False, "unknown_payment_id"
        if rec.used:
            return rec, False, "already_settled"
        if rec.expired:
            return rec, False, "expired"
        if rec.meta.get("kusd"):
            if self.kusd is None or not txid:
                return rec, False, "invalid_payload"
            r = self.kusd.verify(txid, rec.address, rec.amount_sompi, self.kusd.family().authority, rec.baseline)
            if not r.ok:
                return rec, False, "payment_not_found" if r.retryable else "invalid_payment"
            return rec, True, ""
        received = await self.backend.address_received_sompi(rec.address) - rec.baseline
        if received < rec.amount_sompi:
            return rec, False, "insufficient_amount"
        return rec, True, ""

    # ---- facilitator /verify (read-only) ------------------------------------------------
    async def verify(self, payment_payload: dict) -> dict:
        p = x402.extract_payment(payment_payload)
        rec, ok, reason = await self._check(p["payment_id"], p.get("txid"))
        return x402.verify_response(is_valid=ok, payer=p.get("payer"),
                                    invalid_reason=None if ok else reason)

    # ---- facilitator /settle (consumes the record) --------------------------------------
    async def settle(self, payment_payload: dict) -> dict:
        p = x402.extract_payment(payment_payload)
        rec, ok, reason = await self._check(p["payment_id"], p.get("txid"))
        if not ok:
            return x402.settle_response(success=False, network=self.network,
                                        payer=p.get("payer"), error_reason=reason)
        if rec.meta.get("kusd") and not self.store.mark_txid_used(p.get("txid") or ""):
            return x402.settle_response(success=False, network=self.network,
                                        payer=p.get("payer"), error_reason="txid_already_used")
        if not self.store.mark_used(p["payment_id"]):  # lost the race to a parallel settle
            return x402.settle_response(success=False, network=self.network,
                                        payer=p.get("payer"), error_reason="already_settled")
        rec.used = True
        rec.meta["txid"] = p.get("txid")
        return x402.settle_response(success=True, network=self.network,
                                    txid=p.get("txid") or "", payer=p.get("payer"),
                                    amount=str(rec.amount_sompi))

    def supported(self) -> dict:
        return x402.supported(networks=[self.network])

    # ---- FastAPI integration (lazy import so the core stays framework-free) --------------
    def paid(self, amount_sompi: int, *, description: str = "", resource: Optional[str] = None):
        async def dependency(request, response):
            sig = request.headers.get(x402.HDR_SIGNATURE)
            resource_url = resource or str(request.url)
            if not sig:
                pr = await self.demand(amount_sompi, resource_url, description)
                raise _Required402(pr)
            sr = await self.settle(x402.decode_signature_header(sig))
            if not sr["success"]:
                raise _Failed402(sr)
            response.headers[x402.HDR_RESPONSE] = x402.encode_response_header(sr)
            return sr
        return dependency

    def install(self, app, *, facilitator: bool = False) -> None:
        from fastapi.responses import JSONResponse

        @app.exception_handler(_Required402)
        async def _on_required(request, exc: "_Required402"):
            return JSONResponse(status_code=402, content={},
                                headers={x402.HDR_REQUIRED: x402.encode_required_header(exc.pr)})

        @app.exception_handler(_Failed402)
        async def _on_failed(request, exc: "_Failed402"):
            return JSONResponse(status_code=402, content={},
                                headers={x402.HDR_RESPONSE: x402.encode_response_header(exc.sr)})

        if facilitator:
            @app.post("/verify")
            async def _verify(body: dict):
                return JSONResponse(await self.verify(body.get("paymentPayload", {})))

            @app.post("/settle")
            async def _settle(body: dict):
                return JSONResponse(await self.settle(body.get("paymentPayload", {})))

            @app.get("/supported")
            async def _supported():
                return JSONResponse(self.supported())


class _Required402(Exception):
    def __init__(self, pr: dict): self.pr = pr

class _Failed402(Exception):
    def __init__(self, sr: dict): self.sr = sr
