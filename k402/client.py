# Client side of k402: an httpx wrapper that turns HTTP 402 into payment.
#
#   client = Client(payer=HotWallet(private_key))       # kaspa-utxo, non-custodial
#   client = Client(session="s_...")                    # kaspa-session, prepaid
#   r = await client.post("https://api.example/summarize", json={...})
from __future__ import annotations

from typing import Optional, Protocol

import httpx

from .schemes import (PAYMENT_HEADER, SESSION_HEADER, ProtocolError,
                      SessionOffer, UtxoOffer, format_payment_header,
                      parse_offers)


class Payer(Protocol):
    async def pay(self, offer: UtxoOffer) -> str:
        """Pay the offer on-chain; return the txid."""
        ...


class PaymentFailed(Exception):
    """The 402 could not be satisfied. `offers` holds what the server accepts."""

    def __init__(self, message: str, offers: Optional[list] = None):
        self.offers = offers or []
        super().__init__(message)


class Client:
    def __init__(self, payer: Optional[Payer] = None, session: Optional[str] = None,
                 max_kas_per_call: float = 1.0, confirm_retries: int = 5,
                 http: Optional[httpx.AsyncClient] = None):
        self.payer = payer
        self.session = session
        self.max_sompi_per_call = int(max_kas_per_call * 100_000_000)
        self.confirm_retries = confirm_retries
        self.http = http or httpx.AsyncClient(timeout=180)

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.session:
            headers[SESSION_HEADER] = self.session

        r = await self.http.request(method, url, headers=headers, **kwargs)
        if r.status_code != 402:
            return r

        try:
            offers = parse_offers(r.json())
        except (ProtocolError, ValueError) as e:
            raise PaymentFailed(f"server sent 402 but not a k402 body: {e}")

        utxo = next((o for o in offers if isinstance(o, UtxoOffer)), None)
        if utxo is None or self.payer is None:
            hint = next((f"open a session at {o.open}" for o in offers
                         if isinstance(o, SessionOffer)), "no payable offer")
            raise PaymentFailed(
                f"payment required and no payer configured ({hint})", offers)

        if utxo.total_sompi > self.max_sompi_per_call:
            raise PaymentFailed(
                f"offer wants {utxo.total_sompi} sompi, over the "
                f"max_kas_per_call guard of {self.max_sompi_per_call}", offers)

        txid = await self.payer.pay(utxo)
        headers[PAYMENT_HEADER] = format_payment_header(txid, utxo.payment_id)

        # Kaspa accepts in ~1s; retry briefly in case we beat the node to it.
        import asyncio
        for attempt in range(self.confirm_retries):
            r = await self.http.request(method, url, headers=headers, **kwargs)
            if r.status_code != 402:
                return r
            await asyncio.sleep(1.0 + attempt)
        raise PaymentFailed(
            f"paid tx {txid} but server still returns 402 after "
            f"{self.confirm_retries} retries", offers)

    async def get(self, url: str, **kw) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> httpx.Response:
        return await self.request("POST", url, **kw)

    async def aclose(self) -> None:
        await self.http.aclose()
