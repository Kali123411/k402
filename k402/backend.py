# Chain backends answer one question for the verifier: how many sompi has an
# address received? Fresh-address-per-payment makes that equal to its balance.
#
# PnnBackend resolves a public node via the Kaspa Resolver (PNN,
# https://kaspa.aspectron.org/rpc/pnn.html). PNN is dev/test-grade by its own
# docs — point production at your own node with NodeBackend(url=...).
from __future__ import annotations

import asyncio
import time
from typing import Optional, Protocol


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
            if self._client is None or not self._client.is_connected():
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
        if self._client is not None and self._client.is_connected():
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
