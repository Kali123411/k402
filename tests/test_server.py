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
        backend=backend, quote_ttl=600, min_payable_sompi=0)  # test flow mechanics below the floor
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


def test_utxo_offer_respects_min_payable_floor():
    """kaspa-utxo offers must be >= 0.1 KAS (Kaspa anti-spam floor); sub-floor prices
    are quoted at the floor so the payer can actually broadcast."""
    import asyncio
    from k402 import K402
    from k402.addresses import CallbackAddressProvider

    class FakeBackend:
        async def address_received_sompi(self, a): return 0
        async def close(self): pass

    async def run():
        k = K402(address_provider=CallbackAddressProvider(lambda p: "kaspa:addr"),
                 backend=FakeBackend())
        cheap = await k.create_offer(500_000, "cheap call")   # 0.005 KAS requested
        assert cheap.amount_sompi == "10000000"               # quoted at the 0.1 KAS floor
        rich = await k.create_offer(50_000_000, "big call")   # 0.5 KAS requested
        assert rich.amount_sompi == "50000000"                # above floor, unchanged
        assert k.store.get(cheap.payment_id).amount_sompi == 10_000_000
    asyncio.run(run())


def test_reused_address_standing_balance_does_not_auto_verify():
    """SECURITY: a reused address with prior history/balance must NOT auto-satisfy new offers.
    Verification counts only funds received AFTER the offer (delta vs baseline)."""
    import asyncio
    from k402 import K402, format_payment_header, PaymentRequired
    from k402.addresses import StaticAddressProvider

    class FakeBackend:
        def __init__(self, standing): self.received = standing  # address already received a lot
        async def address_received_sompi(self, a): return self.received
        async def close(self): pass

    async def run():
        backend = FakeBackend(standing=5_000_000_000)  # address already has huge history
        k = K402(address_provider=StaticAddressProvider("kaspa:reused"), backend=backend,
                 min_payable_sompi=0)
        offer = await k.create_offer(1000, "call")     # baseline snapshots the 5e9 standing
        hdr = format_payment_header("anytxid", offer.payment_id)
        # no NEW funds since the offer -> must NOT verify despite the huge standing balance
        try:
            await k.verify(hdr, 1000, "call"); assert False, "auto-verified against standing balance!"
        except PaymentRequired as e:
            assert "received 0 of 1000" in e.body["reason"]
        # a real payment (standing + 1000) verifies
        backend.received += 1000
        rec = await k.verify(hdr, 1000, "call")
        assert rec.meta["txid"] == "anytxid"
    asyncio.run(run())


def test_blockbook_coin_mode_flow():
    """K402(coin=...) emits blockbook-utxo offers and verifies against a Blockbook-style backend
    (received atomic units), with the same replay + floor semantics as the Kaspa path."""
    import asyncio
    from k402 import K402, format_payment_header, PaymentRequired
    from k402.addresses import CallbackAddressProvider

    class FakeBB:
        def __init__(self): self.received = {}
        async def address_received_sompi(self, a): return self.received.get(a, 0)
        async def close(self): pass

    async def run():
        backend = FakeBB()
        counter = iter(range(100))
        k = K402(address_provider=CallbackAddressProvider(lambda p: f"tprl1{next(counter)}"),
                 backend=backend, network="testnet", coin="pearl-testnet", decimals=8,
                 min_payable_sompi=100000)
        offer = await k.create_offer(500, "cheap")      # below floor -> quoted at floor
        assert offer.scheme == "blockbook-utxo" and offer.amount == "100000"
        backend.received[offer.pay_to] = 100000
        hdr = format_payment_header("tx1", offer.payment_id, scheme="blockbook-utxo")
        rec = await k.verify(hdr, 500, "cheap")
        assert rec.meta["txid"] == "tx1"
        try:
            await k.verify(hdr, 500, "cheap"); assert False
        except PaymentRequired as e:
            assert "already used" in e.body["reason"]
        # a kaspa-utxo header is rejected in coin mode
        try:
            await k.verify("kaspa-utxo tx p_x", 500, "cheap"); assert False
        except PaymentRequired as e:
            assert "unsupported scheme" in e.body["reason"]

    asyncio.run(run())
