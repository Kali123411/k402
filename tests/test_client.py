import asyncio
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import Depends, FastAPI

from k402 import Client, K402, PaymentFailed, UtxoOffer
from k402.addresses import CallbackAddressProvider


class FakeBackend:
    def __init__(self):
        self.received = {}

    async def address_received_sompi(self, address):
        return self.received.get(address, 0)

    async def close(self):
        pass


class FakePayer:
    """'Pays' by crediting the fake chain, like a wallet whose tx confirmed."""

    def __init__(self, backend):
        self.backend = backend
        self.paid = []

    async def pay(self, offer: UtxoOffer) -> str:
        self.backend.received[offer.pay_to] = int(offer.amount_sompi)
        self.paid.append(offer)
        return "txid_fake"


@pytest.fixture()
def live_server():
    backend = FakeBackend()
    counter = iter(range(10_000))
    k402 = K402(address_provider=CallbackAddressProvider(
        lambda pid: f"kaspa:addr{next(counter)}"), backend=backend)
    app = FastAPI()
    k402.install(app)

    @app.post("/echo")
    async def echo(body: dict, payment=Depends(k402.paid(sompi=1000, description="echo"))):
        return {"echo": body, "txid": payment.meta["txid"]}

    config = uvicorn.Config(app, host="127.0.0.1", port=8402, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield backend
    server.should_exit = True
    t.join(timeout=5)


def test_client_pays_402_and_retries(live_server):
    backend = live_server

    async def run():
        payer = FakePayer(backend)
        client = Client(payer=payer, max_kas_per_call=0.001)
        r = await client.post("http://127.0.0.1:8402/echo", json={"hi": 1})
        await client.aclose()
        return payer, r

    payer, r = asyncio.run(run())
    assert r.status_code == 200
    assert r.json() == {"echo": {"hi": 1}, "txid": "txid_fake"}
    assert len(payer.paid) == 1 and payer.paid[0].amount_sompi == "1000"


def test_client_spend_guard(live_server):
    async def run():
        client = Client(payer=FakePayer(live_server), max_kas_per_call=0.000001)
        try:
            await client.post("http://127.0.0.1:8402/echo", json={})
        finally:
            await client.aclose()

    with pytest.raises(PaymentFailed, match="over the"):
        asyncio.run(run())


def test_client_no_payer(live_server):
    async def run():
        client = Client()
        try:
            await client.post("http://127.0.0.1:8402/echo", json={})
        finally:
            await client.aclose()

    with pytest.raises(PaymentFailed, match="no payer configured"):
        asyncio.run(run())
