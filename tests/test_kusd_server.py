# A provider that accepts kUSD on both wires (k402's own header and x402 V2), verifying each payment
# through the wallet (faked here: no chain).
import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from k402 import K402, PAYMENT_HEADER, x402
from k402.addresses import CallbackAddressProvider
from k402.kusd import Family, VerifyResult
from k402.schemes import SCHEME_KUSD
from k402.x402_server import X402Server

AUTH = "06" * 32
PAYEE = "ea" * 32


class FakeBackend:
    def __init__(self):
        self.received = {}
        self.daa = 624_000_000

    async def address_received_sompi(self, address):
        return self.received.get(address, 0)

    async def daa_score(self):
        return self.daa

    async def close(self):
        pass


class FakeWallet:
    """Chain state as a dict: txid -> (payee, cents, daa); verify answers like kusd_pay does."""

    def __init__(self):
        self.payments = {}
        self.calls = []

    def family(self):
        return Family(authority=AUTH, borrow_guard="00" * 32, ext="ac" * 32, network="testnet-10")

    def payee_xonly(self):
        return PAYEE

    def verify(self, txid, to_xonly, cents, authority, since_daa):
        self.calls.append((txid, to_xonly, cents, authority, since_daa))
        if authority != AUTH:
            return VerifyResult(ok=False, reason="asset mismatch", index=None, daa=None, retryable=False)
        p = self.payments.get(txid)
        if p is None or p[0] != to_xonly or p[1] != cents:
            return VerifyResult(ok=False, reason=f"payment {txid} not indexed yet at the expected box address", index=None, daa=None, retryable=True)
        if p[2] < since_daa:
            return VerifyResult(ok=False, reason="payment is older than the offer", index=None, daa=None, retryable=False)
        return VerifyResult(ok=True, reason="", index=0, daa=p[2], retryable=False)


@pytest.fixture()
def rig():
    backend, wallet = FakeBackend(), FakeWallet()
    counter = iter(range(10_000))
    k402 = K402(address_provider=CallbackAddressProvider(lambda pid: f"kaspa:addr{next(counter)}"),
                backend=backend, network="testnet-10", min_payable_sompi=0, kusd=wallet, kusd_price_cents=5)
    app = FastAPI()
    k402.install(app)

    @app.post("/paid")
    async def paid_route(payment=Depends(k402.paid(sompi=1000, description="summarize"))):
        return {"ok": True, "scheme": payment.meta.get("scheme"), "txid": payment.meta.get("txid")}

    return TestClient(app), backend, wallet, k402


def kusd_offer(client):
    body = client.post("/paid").json()
    return next(o for o in body["accepts"] if o["scheme"] == SCHEME_KUSD)


def test_kusd_offer_in_402_on_both_wires(rig):
    client, backend, wallet, _ = rig
    r = client.post("/paid")
    assert r.status_code == 402
    body = r.json()
    o = next(o for o in body["accepts"] if o["scheme"] == SCHEME_KUSD)
    assert o == {"scheme": "kusd-utxo", "network": "testnet-10", "amount_cents": "5", "pay_to_pubkey": PAYEE,
                 "authority": AUTH, "payment_id": o["payment_id"], "expires": o["expires"], "description": "summarize"}
    assert o["expires"] > time.time()
    # the same offer as x402 V2 in the PAYMENT-REQUIRED header
    pr = x402.decode_required_header(r.headers[x402.HDR_REQUIRED])
    req = next(a for a in pr["accepts"] if x402.is_kusd(a))
    assert (req["scheme"], req["network"], req["amount"], req["asset"], req["payTo"]) == ("exact", "kaspa:testnet-10", "5", AUTH, PAYEE)
    assert req["extra"]["assetSymbol"] == "kUSD" and req["extra"]["decimals"] == 2 and req["extra"]["binding"] == "kusd-holder-v1"
    assert req["extra"]["payment_id"] == o["payment_id"]


def test_kas_and_kusd_offers_coexist(rig):
    client, _, _, _ = rig
    body = client.post("/paid").json()
    schemes = sorted(o["scheme"] for o in body["accepts"])
    assert schemes == ["kaspa-utxo", "kusd-utxo"]
    pr = x402.decode_required_header(client.post("/paid").headers[x402.HDR_REQUIRED])
    assets = sorted(a["asset"] for a in pr["accepts"])
    assert assets == [AUTH, "KAS"]


def pay(client, wallet, offer, txid, cents=None, daa=624_000_100, header=PAYMENT_HEADER):
    wallet.payments[txid] = (PAYEE, cents if cents is not None else int(offer["amount_cents"]), daa)
    return client.post("/paid", headers={PAYMENT_HEADER: f"kusd-utxo {txid} {offer['payment_id']}"})


def test_kusd_payment_served_once(rig):
    client, _, wallet, _ = rig
    o = kusd_offer(client)
    r = pay(client, wallet, o, "d3" * 32)
    assert r.status_code == 200 and r.json() == {"ok": True, "scheme": "kusd-utxo", "txid": "d3" * 32}
    assert wallet.calls[-1] == ("d3" * 32, PAYEE, 5, AUTH, 624_000_000), "verified at the offer's DAA baseline"
    r2 = client.post("/paid", headers={PAYMENT_HEADER: f"kusd-utxo {'d3' * 32} {o['payment_id']}"})
    assert r2.status_code == 402 and "already used" in r2.json()["reason"]


def test_replayed_txid_refused(rig):
    client, _, wallet, _ = rig
    o1 = kusd_offer(client)
    assert pay(client, wallet, o1, "d4" * 32).status_code == 200
    o2 = kusd_offer(client)
    r = client.post("/paid", headers={PAYMENT_HEADER: f"kusd-utxo {'d4' * 32} {o2['payment_id']}"})
    assert r.status_code == 402 and "already used" in r.json()["reason"]


def test_unindexed_payment_is_retry_not_failure(rig):
    client, _, wallet, _ = rig
    o = kusd_offer(client)
    r = client.post("/paid", headers={PAYMENT_HEADER: f"kusd-utxo {'d5' * 32} {o['payment_id']}"})
    assert r.status_code == 402
    body = r.json()
    assert "retry shortly" in body["reason"]
    same = next(x for x in body["accepts"] if x["scheme"] == SCHEME_KUSD)
    assert same["payment_id"] == o["payment_id"], "the same offer comes back, not a fresh one"


def test_wrong_amount_refused(rig):
    client, _, wallet, _ = rig
    o = kusd_offer(client)
    r = pay(client, wallet, o, "d6" * 32, cents=6)
    assert r.status_code == 402


def test_x402_kusd_settle_flow():
    backend, wallet = FakeBackend(), FakeWallet()
    srv = X402Server(address_provider=CallbackAddressProvider(lambda pid: f"kaspa:qq{pid[:6]}"), backend=backend,
                     network=x402.NETWORK_TESTNET, kusd=wallet, kusd_price_cents=5)
    import asyncio
    pr = asyncio.run(srv.demand(1000, "https://p.test/summarize"))
    req = next(a for a in pr["accepts"] if x402.is_kusd(a))
    wallet.payments["d7" * 32] = (PAYEE, 5, 624_000_100)
    pp = x402.payment_payload(accepted=req, txid="d7" * 32, payer="ea" * 32)
    v = asyncio.run(srv.verify(pp))
    assert v["isValid"] is True
    s = asyncio.run(srv.settle(pp))
    assert s["success"] is True and s["transaction"] == "d7" * 32 and s["amount"] == "5"
    again = asyncio.run(srv.settle(pp))
    assert again["success"] is False and again["errorReason"] == "already_settled"
    # the same txid against a fresh requirement is a replay
    pr2 = asyncio.run(srv.demand(1000, "https://p.test/summarize"))
    req2 = next(a for a in pr2["accepts"] if x402.is_kusd(a))
    replay = asyncio.run(srv.settle(x402.payment_payload(accepted=req2, txid="d7" * 32)))
    assert replay["success"] is False and replay["errorReason"] == "txid_already_used"


def test_k402_endpoint_accepts_the_x402_payment_signature_wire(rig):
    # a provider that advertised the offer in PAYMENT-REQUIRED must take the proof back as PAYMENT-SIGNATURE:
    # the registry gate ate a real 10-cent fee on TN10 when it only read X-K402-Payment
    client, _, wallet, _ = rig
    r = client.post("/paid")
    req = next(a for a in x402.decode_required_header(r.headers[x402.HDR_REQUIRED])["accepts"] if x402.is_kusd(a))
    txid = "d7" * 32
    wallet.payments[txid] = (PAYEE, 5, 624_000_100)
    pp = x402.payment_payload(accepted=req, txid=txid, payer="ea" * 32)
    paid = client.post("/paid", headers={x402.HDR_SIGNATURE: x402.encode_signature_header(pp)})
    assert paid.status_code == 200, paid.text
    assert paid.json() == {"ok": True, "scheme": "kusd-utxo", "txid": txid}
    # and the same proof is refused the second time (txid consumed), on either wire
    again = client.post("/paid", headers={x402.HDR_SIGNATURE: x402.encode_signature_header(pp)})
    assert again.status_code == 402 and "already used" in again.json()["reason"]
