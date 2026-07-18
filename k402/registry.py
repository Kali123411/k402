# The service registry: how providers publish k402-payable services and how agents discover them.
#
# A listing is signed by the provider's payee key (proves control of the key that will receive
# payment) and names a capability, endpoint, price, and channel terms. The registry never touches
# money — it's a discovery layer; settlement happens directly between payer and provider. This
# module is the wire format + a thin client; the registry SERVICE is registry_server.py.
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .channel import payer_pubkey_from_privkey, sign_blob, verify_blob

# capability vocabulary is open, but these are the conventional slugs so discovery is predictable.
CAPABILITIES = ("llm:chat", "llm:reason", "llm:code", "summarize", "extract", "classify",
                "rewrite", "embed", "read", "search", "zk-prove", "attest",
                "covenant:compile", "covenant:build", "chain:balance", "chain:utxos", "chain:tx")


@dataclass
class Listing:
    """A provider's advertisement of one k402-payable service. Signed by `payee_pubkey`."""
    capability: str
    endpoint: str
    payee_pubkey: str            # x-only hex — the channel payee + the signer of this listing
    price_usd: float
    network: str = "mainnet"
    schemes: list = field(default_factory=lambda: ["kaspa-channel", "kaspa-utxo"])
    channel_terms: dict = field(default_factory=dict)   # min/max_sompi, maxfee_sompi, min_expiry_daa_delta
    stake_outpoint: Optional[str] = None                # "txid:index" — optional skin-in-the-game
    meta: dict = field(default_factory=dict)            # model, region, latency_ms_p50, ...
    listed_at: int = 0
    sig: str = ""                                       # BIP340 over the canonical body by payee_pubkey

    def canonical(self) -> bytes:
        """Deterministic bytes that get signed — every field except the signature, sorted."""
        body = {k: v for k, v in self.to_dict().items() if k != "sig"}
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "capability": self.capability, "endpoint": self.endpoint,
            "payee_pubkey": self.payee_pubkey, "price_usd": self.price_usd,
            "network": self.network, "schemes": self.schemes,
            "channel_terms": self.channel_terms, "stake_outpoint": self.stake_outpoint,
            "meta": self.meta, "listed_at": self.listed_at,
        }
        if self.sig:
            d["sig"] = self.sig
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Listing":
        return cls(
            capability=d["capability"], endpoint=d["endpoint"], payee_pubkey=d["payee_pubkey"],
            price_usd=float(d["price_usd"]), network=d.get("network", "mainnet"),
            schemes=d.get("schemes", ["kaspa-channel", "kaspa-utxo"]),
            channel_terms=d.get("channel_terms", {}), stake_outpoint=d.get("stake_outpoint"),
            meta=d.get("meta", {}), listed_at=int(d.get("listed_at", 0)), sig=d.get("sig", ""))

    def sign(self, payee_privkey_hex: str) -> "Listing":
        """Sign in place with the payee key; stamps listed_at if unset. Verifies key match."""
        if payer_pubkey_from_privkey(payee_privkey_hex) != self.payee_pubkey:
            raise ValueError("payee_privkey does not match payee_pubkey")
        if not self.listed_at:
            self.listed_at = int(time.time())
        self.sig = sign_blob(payee_privkey_hex, self.canonical())
        return self

    def verify(self) -> bool:
        """True iff the signature is by payee_pubkey over this listing's canonical body."""
        return bool(self.sig) and verify_blob(self.payee_pubkey, self.canonical(), self.sig)


class RegistryClient:
    """Thin async client for a registry service — list, search, fetch a provider, report a close."""

    def __init__(self, base_url: str, http=None):
        import httpx
        self.base = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=30)

    async def list_service(self, listing: Listing) -> dict:
        r = await self._http.post(f"{self.base}/registry/list", json=listing.to_dict())
        return r.json()

    async def search(self, capability: str = "", max_price_usd: Optional[float] = None,
                     min_reputation_kas: float = 0.0, network: str = "", limit: int = 20) -> list:
        params: dict[str, Any] = {"limit": limit}
        if capability:
            params["capability"] = capability
        if max_price_usd is not None:
            params["max_price_usd"] = max_price_usd
        if min_reputation_kas:
            params["min_reputation_kas"] = min_reputation_kas
        if network:
            params["network"] = network
        r = await self._http.get(f"{self.base}/registry/search", params=params)
        return r.json().get("providers", [])

    async def provider(self, payee_pubkey: str) -> dict:
        r = await self._http.get(f"{self.base}/registry/provider/{payee_pubkey}")
        return r.json()

    async def report_settled(self, payee_pubkey: str, close_txid: str) -> dict:
        r = await self._http.post(f"{self.base}/registry/settled",
                                  json={"payee_pubkey": payee_pubkey, "close_txid": close_txid})
        return r.json()

    async def close(self):
        await self._http.aclose()
