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
