import asyncio

import pytest

kaspa = pytest.importorskip("kaspa")

from k402 import PnnBackend


def test_reused_connection_survives_second_call():
    """Regression: kaspa>=2.0 RpcClient.is_connected is a property; calling it as a
    method made every SECOND backend query raise TypeError."""
    async def run():
        backend = PnnBackend()
        addr = kaspa.Keypair.random().to_address("mainnet").to_string()
        a = await backend.address_received_sompi(addr)   # connects
        b = await backend.address_received_sompi(addr)   # reuses cached client
        await backend.close()
        return a, b

    assert asyncio.run(run()) == (0, 0)
