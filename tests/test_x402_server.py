# End-to-end test of the native x402 V2 Kaspa server, with a fake backend (no node needed).
# Proves: demand builds a real V2 PaymentRequired + mints a record; settle confirms receipt against
# the SERVER's record (not the client's echoed amount); baseline reuse-safety carries over; a settled
# payment can't be replayed.
import asyncio

from k402 import x402
from k402.store import MemoryStore
from k402.x402_server import X402Server


class FakeBackend:
    def __init__(self):
        self.received = {}
    async def address_received_sompi(self, address):
        return self.received.get(address, 0)
    async def close(self):
        pass


class SeqAddresses:
    """Deterministic per-payment address."""
    def next_address(self, payment_id):
        return f"kaspa:qq{payment_id[:10]}"


class FixedAddress:
    def __init__(self, addr):
        self.addr = addr
    def next_address(self, payment_id):
        return self.addr


def test_demand_builds_v2_payment_required_and_record():
    srv = X402Server(SeqAddresses(), FakeBackend(), MemoryStore(), service_name="acme-infer")
    pr = asyncio.run(srv.demand(1_500_000, "https://api.acme.ai/v1/run", "LLM inference"))
    assert pr["x402Version"] == 2
    assert pr["resource"]["url"] == "https://api.acme.ai/v1/run"
    req = pr["accepts"][0]
    assert req["scheme"] == "exact"
    assert req["network"] == x402.NETWORK_MAINNET and ":" in req["network"]
    assert req["amount"] == "1500000"
    assert req["payTo"].startswith("kaspa:qq")
    # the offer minted a server-side record keyed by payment_id
    assert srv.store.get(req["extra"]["payment_id"]) is not None


def test_full_flow_402_pay_verify_settle_noreuse():
    be = FakeBackend()
    srv = X402Server(SeqAddresses(), be, MemoryStore())
    pr = asyncio.run(srv.demand(1_500_000, "https://api/x"))
    req = pr["accepts"][0]
    pay_to = req["payTo"]

    # client picks the requirement, "pays", signs a payload with the txid
    pp = x402.payment_payload(accepted=req, txid="a1db5b43add0", payer="kaspa:qqpayer")

    # settle before the money lands -> failure (V2 codes)
    sr = asyncio.run(srv.settle(pp))
    assert sr["success"] is False and sr["errorReason"] == "insufficient_amount"
    assert sr["transaction"] == ""

    # money lands at the per-offer address
    be.received[pay_to] = 1_500_000

    # /verify is read-only and now valid
    v = asyncio.run(srv.verify(pp))
    assert v["isValid"] is True and v["payer"] == "kaspa:qqpayer"

    # /settle succeeds, echoes the tx + settled amount
    sr = asyncio.run(srv.settle(pp))
    assert sr["success"] is True
    assert sr["transaction"] == "a1db5b43add0"
    assert sr["network"] == x402.NETWORK_MAINNET
    assert sr["amount"] == "1500000"

    # replay is rejected
    sr2 = asyncio.run(srv.settle(pp))
    assert sr2["success"] is False and sr2["errorReason"] == "already_settled"


def test_baseline_blocks_standing_balance():
    be = FakeBackend()
    be.received["kaspa:qfixed"] = 5_000_000          # address already holds funds
    srv = X402Server(FixedAddress("kaspa:qfixed"), be, MemoryStore())
    pr = asyncio.run(srv.demand(1_000_000, "https://api/x"))
    pp = x402.payment_payload(accepted=pr["accepts"][0], txid="tx1")

    # the 5M standing balance must NOT auto-satisfy the 1M offer (baseline snapshot)
    sr = asyncio.run(srv.settle(pp))
    assert sr["success"] is False and sr["errorReason"] == "insufficient_amount"

    # a real 1M payment after the offer (total 6M -> delta 1M) settles
    be.received["kaspa:qfixed"] = 6_000_000
    sr = asyncio.run(srv.settle(pp))
    assert sr["success"] is True


def test_untrusted_client_amount_is_ignored():
    be = FakeBackend()
    srv = X402Server(SeqAddresses(), be, MemoryStore())
    pr = asyncio.run(srv.demand(2_000_000, "https://api/x"))
    req = pr["accepts"][0]
    pay_to = req["payTo"]

    # attacker echoes a tampered lower amount in `accepted`
    tampered = dict(req, amount="1")
    pp = x402.payment_payload(accepted=tampered, txid="tx")

    be.received[pay_to] = 1_000_000                  # paid only 1M, real offer was 2M
    sr = asyncio.run(srv.settle(pp))
    # server checks its OWN record (2M), not the client's "1" -> rejected
    assert sr["success"] is False and sr["errorReason"] == "insufficient_amount"


def test_supported_advertises_kaspa_exact():
    srv = X402Server(SeqAddresses(), FakeBackend(), MemoryStore())
    sup = srv.supported()
    k = sup["kinds"][0]
    assert k["x402Version"] == 2 and k["scheme"] == "exact" and ":" in k["network"]


# ---- kUSD on the pure x402 server, and the FastAPI glue ----
def test_paid_dependency_works_under_fastapi():
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    srv = X402Server(SeqAddresses(), FakeBackend(), MemoryStore())
    app = FastAPI()
    srv.install(app)

    @app.post("/x")
    async def x(sr=Depends(srv.paid(1000))):
        return {"ok": True, "tx": sr["transaction"]}
    c = TestClient(app)
    r = c.post("/x")
    assert r.status_code == 402 and x402.HDR_REQUIRED in r.headers, r.text   # not 422: request/response are injected
    req = x402.decode_required_header(r.headers[x402.HDR_REQUIRED])["accepts"][0]
    # a failed settle still tells the client how to pay again
    bad = c.post("/x", headers={x402.HDR_SIGNATURE: x402.encode_signature_header(x402.payment_payload(accepted=req, txid="aa" * 32))})
    assert bad.status_code == 402 and x402.HDR_RESPONSE in bad.headers and x402.HDR_REQUIRED in bad.headers


def test_kusd_records_are_shared_with_k402_and_txids_are_canonical():
    import asyncio
    from k402 import K402
    from k402.addresses import CallbackAddressProvider
    from k402.kusd import Family, VerifyResult
    from k402.kusd_keys import derive_payee

    class Wallet:
        payments, claims = {}, []
        def family(self): return Family(authority="06" * 32, borrow_guard="00" * 32, ext="ac" * 32, network="testnet-10")
        def payee_xonly(self): return "ea" * 32
        def verify(self, txid, to, cents, authority, since):
            p = self.payments.get((txid, to, cents))
            return VerifyResult(ok=p is not None, reason="" if p else "not indexed yet", index=0 if p else None, daa=p, retryable=p is None)
        def claim(self, txid, index, cents, tweak): self.claims.append((txid, index, cents, tweak)); return {"ok": True}

    class Be(FakeBackend):
        async def daa_score(self): return 7
    w, be, store = Wallet(), Be(), MemoryStore()
    srv = X402Server(SeqAddresses(), be, store, network=x402.NETWORK_TESTNET, kusd=w, kusd_price_cents=5)
    k402 = K402(address_provider=CallbackAddressProvider(lambda pid: "kaspa:a"), backend=be, network="testnet-10", store=store, kusd=w, kusd_price_cents=5)
    req = next(a for a in asyncio.run(srv.demand(1000, "https://p/x"))["accepts"] if x402.is_kusd(a))
    pid = req["extra"]["payment_id"]
    assert req["payTo"] == derive_payee("ea" * 32, pid)
    w.payments[("ab" * 32, req["payTo"], 5)] = 9
    # one store, two front doors: the record X402Server minted settles through K402's header path
    rec = asyncio.run(k402.verify(f"kusd-utxo {'AB' * 32} {pid}", 1000, cents=5))
    assert rec.meta["txid"] == "ab" * 32 and w.claims[-1][0] == "ab" * 32
    # and the x402 settle of the same txid, in any case, is a replay
    req2 = next(a for a in asyncio.run(srv.demand(1000, "https://p/x"))["accepts"] if x402.is_kusd(a))
    w.payments[("ab" * 32, req2["payTo"], 5)] = 9
    s = asyncio.run(srv.settle(x402.payment_payload(accepted=req2, txid="AB" * 32)))
    assert s["success"] is False and s["errorReason"] == "txid_already_used"
    import pytest
    with pytest.raises(ValueError, match="daa_score"):
        X402Server(SeqAddresses(), FakeBackend(), MemoryStore(), kusd=w, kusd_price_cents=5)
