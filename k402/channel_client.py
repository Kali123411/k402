# Payer side of kaspa-channel + the service exchange: discover providers, route to one, open a
# channel, and pay per call with vouchers. The mirror of channel_server.ChannelManager.
#
#   payer = ChannelPayer(payer_privkey=KEY, opener=SubprocessChannelOpener(bin, cwd),
#                        backend=NodeBackend(...), registry_url="https://registry.example")
#   providers = await payer.discover("summarize", max_price_usd=0.005)
#   r = await payer.pay(providers[0], "/summarize", {"text": "..."}, price_sompi=3_000_000)
#
# Routing is client-side: discover() returns providers ranked by the registry (reputation, then
# price), and pay() opens/reuses a direct channel to the chosen provider — the registry and any
# operator are never in the money path.
from __future__ import annotations

import os
import subprocess
from typing import Optional, Protocol
from urllib.parse import urlsplit

import httpx

from .channel import (format_channel_header, payer_pubkey_from_privkey, sign_voucher)
from .registry import RegistryClient


class ChannelOpener(Protocol):
    """Opens+funds a channel covenant on-chain. Keeps the payer key local."""
    def open(self, *, payer_privkey: str, payee_pubkey: str, expiry_daa: int,
             amount_sompi: int, maxfee: int) -> tuple:
        """Returns (channel_id, open_txid)."""
        ...


class SubprocessChannelOpener:
    """ChannelOpener backed by the `channel_cycle` binary (reference impl; selects a funding UTXO
    from the payer's own address and broadcasts the open)."""

    def __init__(self, bin_path: str, cwd: str, network: str = "mainnet",
                 node: str = "localhost:16110", open_fee: int = 3_000_000):
        self.bin_path, self.cwd, self.network, self.node = bin_path, cwd, network, node
        self.open_fee = open_fee

    def _run(self, env: dict, timeout: int = 90) -> str:
        out = subprocess.run([self.bin_path], cwd=self.cwd, capture_output=True, text=True,
                             timeout=timeout,
                             env={**os.environ, "NETWORK": self.network, "NODE": self.node, **env})
        if out.returncode != 0:
            raise RuntimeError(f"channel_cycle: {out.stderr.strip()[:200] or out.stdout.strip()[:200]}")
        return out.stdout.strip()

    def _fund_utxo(self, payer_privkey: str, need: int) -> tuple:
        rows = [dict(kv.split("=") for kv in l.split())
                for l in self._run({"MODE": "utxos", "AGENT_KEY": payer_privkey}).splitlines()
                if "FUND_AMOUNT" in l]
        rows = [d for d in rows if int(d["FUND_AMOUNT"]) >= need]
        if not rows:
            raise RuntimeError(f"no funding UTXO >= {need} sompi on the payer address")
        best = max(rows, key=lambda d: int(d["FUND_AMOUNT"]))
        return best["FUND_TXID"], best["FUND_INDEX"], best["FUND_AMOUNT"]

    def open(self, *, payer_privkey, payee_pubkey, expiry_daa, amount_sompi, maxfee):
        txid, idx, amt = self._fund_utxo(payer_privkey, amount_sompi + self.open_fee)
        out = self._run({"MODE": "open", "AGENT_KEY": payer_privkey, "SERVICE_PUBKEY": payee_pubkey,
                         "CHANNEL": str(amount_sompi), "EXPIRY_DAA": str(expiry_daa), "MAXFEE": str(maxfee),
                         "FUND_TXID": txid, "FUND_INDEX": idx, "FUND_AMOUNT": amt})
        if "covid (channel id) " not in out:
            raise RuntimeError(f"open failed: {out[-200:]}")
        return out.split("covid (channel id) ")[1].split()[0], out.split("txid ")[1].split()[0]


def _base_url(endpoint: str) -> str:
    """The provider's root (where /channel/* lives) from a capability endpoint URL."""
    p = urlsplit(endpoint)
    return f"{p.scheme}://{p.netloc}"


class ChannelPayer:
    def __init__(self, payer_privkey: str, opener: ChannelOpener, backend,
                 registry_url: Optional[str] = None, network: str = "mainnet",
                 channel_size_sompi: int = 150_000_000, http: Optional[httpx.AsyncClient] = None):
        self.payer_privkey = payer_privkey
        self.payer_pubkey = payer_pubkey_from_privkey(payer_privkey)
        self.opener = opener
        self.backend = backend
        self.network = network
        self.channel_size = channel_size_sompi
        self.http = http or httpx.AsyncClient(timeout=180)
        self._registry = RegistryClient(registry_url, http=self.http) if registry_url else None
        self._channels: dict = {}   # provider base_url -> {channel_id, total, maxfee}

    # -------- discovery + client-side routing --------
    async def discover(self, capability: str = "", max_price_usd: Optional[float] = None,
                       min_reputation_kas: float = 0.0, limit: int = 20) -> list:
        """Ranked providers from the registry (reputation, then price). The agent picks — routing
        is client-side. Returns the raw provider dicts (endpoint, payee_pubkey, channel_terms, ...)."""
        if not self._registry:
            raise RuntimeError("no registry_url configured")
        return await self._registry.search(capability=capability, max_price_usd=max_price_usd,
                                            min_reputation_kas=min_reputation_kas, limit=limit)

    # -------- open (or reuse) a channel to a provider --------
    async def open_channel(self, provider: dict) -> str:
        """Open a channel to `provider` (a discover() result) and register it. Returns channel_id."""
        base = _base_url(provider["endpoint"])
        terms = provider.get("channel_terms") or {}
        payee = provider["payee_pubkey"]
        maxfee = int(terms.get("maxfee_sompi", 5_000_000))
        size = max(int(terms.get("min_sompi", 0)), min(self.channel_size,
                   int(terms.get("max_sompi", self.channel_size))))
        expiry = await self.backend.daa_score() + int(terms.get("min_expiry_daa_delta", 864_000)) + 100_000
        channel_id, open_txid = self.opener.open(payer_privkey=self.payer_privkey, payee_pubkey=payee,
                                                 expiry_daa=expiry, amount_sompi=size, maxfee=maxfee)
        # register with the provider (retry while the open tx settles into the utxo index)
        import asyncio
        for _ in range(8):
            r = await self.http.post(f"{base}/channel/open", json={
                "payer_pubkey": self.payer_pubkey, "expiry_daa": expiry, "txid": open_txid, "index": 0})
            if r.status_code == 200:
                self._channels[base] = {"channel_id": channel_id, "total": 0, "maxfee": maxfee}
                return channel_id
            if "not found" not in r.text:
                raise RuntimeError(f"provider rejected the channel: {r.text}")
            await asyncio.sleep(3)
        raise RuntimeError(f"provider never saw the open tx {open_txid}")

    # -------- pay a provider per call --------
    async def pay(self, provider: dict, path: str, json_body: Optional[dict] = None,
                  price_sompi: int = 3_000_000) -> httpx.Response:
        """Pay one call to `provider` over a channel (opened on demand), advancing the voucher total
        by `price_sompi`. `path` is relative to the provider's root."""
        base = _base_url(provider["endpoint"])
        if base not in self._channels:
            await self.open_channel(provider)
        ch = self._channels[base]
        ch["total"] += price_sompi
        voucher = sign_voucher(self.payer_privkey, ch["channel_id"], ch["total"])
        header = format_channel_header(ch["channel_id"], ch["total"], voucher)
        url = base + (path if path.startswith("/") else "/" + path)
        r = await self.http.post(url, json=json_body or {}, headers={"X-K402-Payment": header})
        if r.status_code == 402:      # roll back the local total so the next attempt re-signs cleanly
            ch["total"] -= price_sompi
        return r

    async def aclose(self):
        await self.http.aclose()
