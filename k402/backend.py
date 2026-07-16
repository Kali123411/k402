# Chain backends answer one question for the verifier: how many atomic units has an
# address received? Fresh-address-per-payment makes that equal to its balance.
#
# PnnBackend resolves a public Kaspa node via the Kaspa Resolver (PNN,
# https://kaspa.aspectron.org/rpc/pnn.html). PNN is dev/test-grade by its own
# docs — point production at your own node with NodeBackend(url=...).
# BlockbookBackend serves any Bitcoin-family UTXO chain behind a Blockbook indexer
# (Bitcoin, Litecoin, Dogecoin, Bitcoin Cash, Dash, transparent Zcash, Pearl…).
#
# NOTE: the interface method is named `address_received_sompi` for historical reasons
# (Kaspa's atomic unit); it returns atomic units in whatever chain the backend serves.
from __future__ import annotations

import asyncio
import time
from typing import Optional, Protocol

import httpx


class ChainBackend(Protocol):
    async def address_received_sompi(self, address: str) -> int: ...
    async def close(self) -> None: ...


def _require_kaspa():
    try:
        import kaspa
        return kaspa
    except ImportError as e:
        raise ImportError(
            "chain backends need the kaspa SDK: pip install 'k402[kaspa]'") from e


class _RpcBackend:
    """Shared wRPC client lifecycle for resolver- and url-based backends."""

    def __init__(self, network: str = "mainnet", url: Optional[str] = None):
        self.network = network
        self.url = url
        self._client = None
        self._lock = asyncio.Lock()

    async def _rpc(self):
        kaspa = _require_kaspa()
        async with self._lock:
            # RpcClient.is_connected is a property in kaspa>=2.0, not a method
            if self._client is None or not self._client.is_connected:
                if self.url:
                    self._client = kaspa.RpcClient(url=self.url)
                else:
                    self._client = kaspa.RpcClient(
                        resolver=kaspa.Resolver(), network_id=self.network)
                await self._client.connect()
            return self._client

    async def address_received_sompi(self, address: str) -> int:
        rpc = await self._rpc()
        resp = await rpc.get_balance_by_address({"address": address})
        return int(resp["balance"])

    async def utxos(self, address: str) -> list:
        rpc = await self._rpc()
        resp = await rpc.get_utxos_by_addresses({"addresses": [address]})
        return resp["entries"]

    async def submit_transaction(self, pending) -> str:
        rpc = await self._rpc()
        return await pending.submit(rpc)

    async def wait_for_payment(self, address: str, amount_sompi: int,
                               timeout: float = 120.0, poll: float = 1.0) -> bool:
        """Poll until `address` has received `amount_sompi` (Kaspa confirms ~1s,
        so polling at 1s is adequate; a utxos-changed subscription can replace
        this later without changing callers)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.address_received_sompi(address) >= amount_sompi:
                return True
            await asyncio.sleep(poll)
        return False

    async def close(self) -> None:
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
        self._client = None


class PnnBackend(_RpcBackend):
    """Public Node Network via the Kaspa Resolver. Dev/test-grade."""

    def __init__(self, network: str = "mainnet"):
        super().__init__(network=network)


class NodeBackend(_RpcBackend):
    """Your own node's wRPC endpoint (e.g. ws://127.0.0.1:17110). Production."""

    def __init__(self, url: str, network: str = "mainnet"):
        super().__init__(network=network, url=url)


class BlockbookBackend:
    """Verifier for any Bitcoin-family UTXO chain served by a Blockbook indexer.

    One adapter covers Bitcoin, Litecoin, Dogecoin, Bitcoin Cash, Dash, transparent Zcash,
    Pearl, and any other coin Blockbook indexes — just pass its API base URL, e.g.
    BlockbookBackend("https://blockbook.pearlresearch.ai"). Verification asks the indexer
    "how much has this address received" via GET /api/v2/address/{addr} -> totalReceived
    (atomic units), which is exactly the k402 primitive. Watch-only; holds no keys.
    """

    def __init__(self, base_url: str, min_confirmations: int = 0,
                 http: Optional[httpx.AsyncClient] = None, user_agent: str = "k402"):
        self.base = base_url.rstrip("/")
        self.min_confirmations = min_confirmations
        # public Blockbook instances (e.g. Trezor's) sit behind Cloudflare and 403 requests with
        # no User-Agent, so always send one.
        self._http = http or httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": user_agent})

    async def _address(self, address: str) -> dict:
        r = await self._http.get(f"{self.base}/api/v2/address/{address}",
                                 params={"details": "basic"})
        r.raise_for_status()
        return r.json()

    async def address_received_sompi(self, address: str) -> int:
        """Total atomic units this address has ever received. With min_confirmations>0,
        subtracts still-unconfirmed receipts so 0-conf payments don't count as final."""
        d = await self._address(address)
        received = int(d.get("totalReceived", 0) or 0)
        if self.min_confirmations > 0:
            # unconfirmedBalance is signed; a positive value is not-yet-confirmed inflow
            unconf = int(d.get("unconfirmedBalance", 0) or 0)
            if unconf > 0:
                received -= unconf
        return received

    async def wait_for_payment(self, address: str, amount_atomic: int,
                               timeout: float = 120.0, poll: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.address_received_sompi(address) >= amount_atomic:
                return True
            await asyncio.sleep(poll)
        return False

    async def status(self) -> dict:
        """Blockbook + backend sync info (coin name, decimals, best height)."""
        r = await self._http.get(f"{self.base}/api/v2")
        r.raise_for_status()
        return r.json()

    async def close(self) -> None:
        await self._http.aclose()
