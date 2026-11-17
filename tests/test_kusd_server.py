# A provider that accepts kUSD on both wires (k402's own header and x402 V2), verifying each payment
# through the wallet (faked here: no chain).
import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from k402 import K402, PAYMENT_HEADER, x402
from k402.addresses import CallbackAddressProvider
from k402.kusd import Family, KusdError, VerifyResult
from k402.kusd_keys import derive_payee, offer_tweak
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
        self.claims = []
        self.down = False

    def family(self):
        if self.down:
            raise KusdError("node unreachable")
        return Family(authority=AUTH, borrow_guard="00" * 32, ext="ac" * 32, network="testnet-10")

    def payee_xonly(self):
        return PAYEE

    def claim(self, txid, index, cents, tweak):
        self.claims.append((txid, index, cents, tweak))
        return {"ok": True}

    def verify(self, txid, to_xonly, cents, authority, since_daa):
        self.calls.append((txid, to_xonly, cents, authority, since_daa))
        if self.down:
            raise KusdError("node unreachable")
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

    @app.post("/expensive")
    async def expensive_route(payment=Depends(k402.paid(sompi=100_000, description="reason", cents=500))):
        return {"ok": True, "cents": payment.amount_sompi}

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
    # paid to the provider's key tweaked by the payment_id: a box binds to its offer, nobody can claim another
    # payer's box with their own payment_id
    pay_to = derive_payee(PAYEE, o["payment_id"])
    assert pay_to != PAYEE
    assert o == {"scheme": "kusd-utxo", "network": "testnet-10", "amount_cents": "5", "pay_to_pubkey": pay_to,
                 "authority": AUTH, "payment_id": o["payment_id"], "expires": o["expires"], "description": "summarize"}
    assert o["expires"] > time.time()
    # the same offer as x402 V2 in the PAYMENT-REQUIRED header
    pr = x402.decode_required_header(r.headers[x402.HDR_REQUIRED])
    req = next(a for a in pr["accepts"] if x402.is_kusd(a))
    assert (req["scheme"], req["network"], req["amount"], req["asset"], req["payTo"]) == ("exact", "kaspa:testnet-10", "5", AUTH, pay_to)
    assert req["extra"]["assetSymbol"] == "kUSD" and req["extra"]["decimals"] == 2 and req["extra"]["binding"] == "kusd-holder-v1"
    assert req["extra"]["payment_id"] == o["payment_id"]
    # the remaining life is measured a moment after the offer was issued: allow the clock to tick between the two
    assert 0 < req["maxTimeoutSeconds"] <= o["expires"] - int(time.time()) + 2, "the timeout is the quote's remaining life"


def test_kas_and_kusd_offers_coexist(rig):
    client, _, _, _ = rig
    body = client.post("/paid").json()
    schemes = sorted(o["scheme"] for o in body["accepts"])
    assert schemes == ["kaspa-utxo", "kusd-utxo"]
    pr = x402.decode_required_header(client.post("/paid").headers[x402.HDR_REQUIRED])
    assets = sorted(a["asset"] for a in pr["accepts"])
    assert assets == [AUTH, "KAS"]


def pay(client, wallet, offer, txid, cents=None, daa=624_000_100, path="/paid"):
    wallet.payments[txid] = (offer["pay_to_pubkey"], cents if cents is not None else int(offer["amount_cents"]), daa)
    return client.post(path, headers={PAYMENT_HEADER: f"kusd-utxo {txid} {offer['payment_id']}"})


def test_kusd_payment_served_once(rig):
    client, _, wallet, _ = rig
    o = kusd_offer(client)
    r = pay(client, wallet, o, "d3" * 32)
    assert r.status_code == 200 and r.json() == {"ok": True, "scheme": "kusd-utxo", "txid": "d3" * 32}
    assert wallet.calls[-1] == ("d3" * 32, o["pay_to_pubkey"], 5, AUTH, 624_000_000), "verified at the offer's DAA baseline"
    # the wallet is told about the box it can now spend with the offer's tweak
    assert wallet.claims == [("d3" * 32, 0, 5, offer_tweak(o["payment_id"]))]
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
    assert req["payTo"] == derive_payee(PAYEE, req["extra"]["payment_id"])
    wallet.payments["d7" * 32] = (req["payTo"], 5, 624_000_100)
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
    wallet.payments[txid] = (req["payTo"], 5, 624_000_100)
    pp = x402.payment_payload(accepted=req, txid=txid, payer="ea" * 32)
    paid = client.post("/paid", headers={x402.HDR_SIGNATURE: x402.encode_signature_header(pp)})
    assert paid.status_code == 200, paid.text
    assert paid.json() == {"ok": True, "scheme": "kusd-utxo", "txid": txid}
    # and the same proof is refused the second time (txid consumed), on either wire
    again = client.post("/paid", headers={x402.HDR_SIGNATURE: x402.encode_signature_header(pp)})
    assert again.status_code == 402 and "already used" in again.json()["reason"]


def test_txid_is_canonical_hex_so_case_games_are_not_a_replay(rig):
    client, _, wallet, _ = rig
    o = kusd_offer(client)
    assert pay(client, wallet, o, "d8" * 32).status_code == 200
    o2 = kusd_offer(client)
    wallet.payments["D8" * 32] = (o2["pay_to_pubkey"], 5, 624_000_100)   # the fake matches case-sensitively; kusd_pay ignores case
    r = client.post("/paid", headers={PAYMENT_HEADER: f"kusd-utxo {'D8' * 32} {o2['payment_id']}"})
    assert r.status_code == 402 and "already used" in r.json()["reason"]
    bad = client.post("/paid", headers={PAYMENT_HEADER: f"kusd-utxo {'zz' * 32} {o2['payment_id']}"})
    assert bad.status_code == 402 and "64 hex" in bad.json()["reason"]
    assert wallet.calls[-1][0] == "d8" * 32, "the wallet never saw the non-hex txid"


def test_kusd_payment_must_cover_the_routes_own_price(rig):
    # a 5-cent offer from /paid is not a ticket to /expensive (500 cents), and /expensive quotes its own price
    client, _, wallet, _ = rig
    o = kusd_offer(client)
    r = pay(client, wallet, o, "d9" * 32, path="/expensive")
    assert r.status_code == 402 and "500" in r.json()["reason"] and "5 " in r.json()["reason"] + " "
    quoted = next(x for x in client.post("/expensive").json()["accepts"] if x["scheme"] == SCHEME_KUSD)
    assert quoted["amount_cents"] == "500"
    junk = client.post("/expensive", headers={PAYMENT_HEADER: "kaspa-utxo zz zz"})
    assert next(x for x in junk.json()["accepts"] if x["scheme"] == SCHEME_KUSD)["amount_cents"] == "500"


def test_wallet_trouble_is_a_retryable_402_never_a_500(rig):
    client, _, wallet, _ = rig
    o = kusd_offer(client)
    wallet.down = True
    r = client.post("/paid", headers={PAYMENT_HEADER: f"kusd-utxo {'da' * 32} {o['payment_id']}"})
    assert r.status_code == 402 and "retry" in r.json()["reason"]
    assert next(x for x in r.json()["accepts"] if x["scheme"] == SCHEME_KUSD)["payment_id"] == o["payment_id"]
    # with the wallet down an unpaid request still gets its KAS offer, just no kUSD one
    unpaid = client.post("/paid")
    assert unpaid.status_code == 402 and [x["scheme"] for x in unpaid.json()["accepts"]] == ["kaspa-utxo"]


def test_kas_header_cannot_target_a_kusd_record(rig):
    client, _, wallet, _ = rig
    o = kusd_offer(client)
    r = client.post("/paid", headers={PAYMENT_HEADER: f"kaspa-utxo {'db' * 32} {o['payment_id']}"})
    assert r.status_code == 402 and "scheme" in r.json()["reason"]


def test_a_payment_that_loses_the_payment_id_race_is_not_burned(rig):
    # two real boxes presented for one payment_id: the loser's txid stays unconsumed for a fresh offer
    client, _, wallet, _ = rig
    o = kusd_offer(client)
    assert pay(client, wallet, o, "dc" * 32).status_code == 200
    r = pay(client, wallet, o, "dd" * 32)
    assert r.status_code == 402 and "payment_id already used" in r.json()["reason"]
    o2 = kusd_offer(client)
    assert pay(client, wallet, o2, "dd" * 32).status_code == 200


def test_kusd_needs_a_daa_baseline_and_a_known_network():
    class NoDaa:
        async def address_received_sompi(self, address):
            return 0
    with pytest.raises(ValueError, match="daa_score"):
        K402(address_provider=CallbackAddressProvider(lambda pid: "kaspa:a"), backend=NoDaa(), network="testnet-10",
             kusd=FakeWallet(), kusd_price_cents=5)
    with pytest.raises(ValueError, match="network"):
        x402.network_id("testnet-11")
    assert (x402.network_id("testnet-10"), x402.network_id("mainnet")) == (x402.NETWORK_TESTNET, x402.NETWORK_MAINNET)
