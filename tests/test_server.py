import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from k402 import (K402, MemoryStore, PAYMENT_HEADER, SqliteStore,
                  StaticAddressProvider, format_payment_header)
from k402.addresses import CallbackAddressProvider


class FakeBackend:
    """Chain stub: address -> sompi received."""

    def __init__(self):
        self.received = {}

    async def address_received_sompi(self, address):
        return self.received.get(address, 0)

    async def close(self):
        pass


@pytest.fixture()
def rig():
    backend = FakeBackend()
    counter = iter(range(10_000))
    k402 = K402(
        address_provider=CallbackAddressProvider(lambda pid: f"kaspa:addr{next(counter)}"),
        backend=backend, quote_ttl=600)
    app = FastAPI()
    k402.install(app)

    @app.post("/paid")
    async def paid_route(payment=Depends(k402.paid(sompi=1000, description="test"))):
        return {"ok": True, "txid": payment.meta["txid"]}

    return TestClient(app), backend, k402


def get_offer(client):
    r = client.post("/paid")
    assert r.status_code == 402
    body = r.json()
    assert body["k402"] == "0.1"
    return next(o for o in body["accepts"] if o["scheme"] == "kaspa-utxo")


def test_unpaid_gets_protocol_402(rig):
    client, _, _ = rig
    offer = get_offer(client)
    assert offer["amount_sompi"] == "1000"
    assert offer["pay_to"].startswith("kaspa:")
    assert offer["expires"] > time.time()


def test_fresh_address_per_offer(rig):
    client, _, _ = rig
    assert get_offer(client)["pay_to"] != get_offer(client)["pay_to"]


def test_paid_flow_and_replay(rig):
    client, backend, _ = rig
    offer = get_offer(client)
    backend.received[offer["pay_to"]] = 1000  # simulate on-chain payment

    hdr = {PAYMENT_HEADER: format_payment_header("tx1", offer["payment_id"])}
    r = client.post("/paid", headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "txid": "tx1"}

    # replay of a consumed payment_id must re-402
    r = client.post("/paid", headers=hdr)
    assert r.status_code == 402
    assert "already used" in r.json()["reason"]


def test_underpayment_rejected(rig):
    client, backend, _ = rig
    offer = get_offer(client)
    backend.received[offer["pay_to"]] = 999
    r = client.post("/paid", headers={
        PAYMENT_HEADER: format_payment_header("tx1", offer["payment_id"])})
    assert r.status_code == 402
    assert "received 999 of 1000" in r.json()["reason"]


def test_unknown_and_malformed_headers(rig):
    client, _, _ = rig
    r = client.post("/paid", headers={PAYMENT_HEADER: "kaspa-utxo tx p_nope"})
    assert r.status_code == 402 and "unknown payment_id" in r.json()["reason"]
    r = client.post("/paid", headers={PAYMENT_HEADER: "garbage"})
    assert r.status_code == 402


def test_expired_quote_rejected(rig):
    client, backend, k402 = rig
    k402.quote_ttl = -1  # already expired at creation
    offer = get_offer(client)
    backend.received[offer["pay_to"]] = 1000
    r = client.post("/paid", headers={
        PAYMENT_HEADER: format_payment_header("tx1", offer["payment_id"])})
    assert r.status_code == 402 and "expired" in r.json()["reason"]


def test_sqlite_store_roundtrip(tmp_path):
    from k402 import PaymentRecord
    store = SqliteStore(str(tmp_path / "p.db"))
    store.create(PaymentRecord("p_1", "kaspa:a", 500, int(time.time()) + 60))
    rec = store.get("p_1")
    assert rec.amount_sompi == 500 and not rec.used
    assert store.mark_used("p_1") is True
    assert store.mark_used("p_1") is False  # atomic single-use
    assert store.get("p_1").used


def test_memory_store_single_use():
    from k402 import PaymentRecord
    store = MemoryStore()
    store.create(PaymentRecord("p_1", "kaspa:a", 500, int(time.time()) + 60))
    assert store.mark_used("p_1") and not store.mark_used("p_1")
