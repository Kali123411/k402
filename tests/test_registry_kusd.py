# The registry in kUSD: listings carry currency / price_cents without disturbing KAS signatures, search filters
# and ranks per currency (USD value at the oracle price when mixed), settled reports in cents are verified
# through the wallet and refused once the box is spent, and listing in kUSD costs a kUSD fee.
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from k402.channel import payer_pubkey_from_privkey
from k402.kusd import Family, VerifyResult
from k402.registry import Listing
from k402.registry_server import create_registry_app, rank_key

A_PRIV, B_PRIV = "a1" * 32, "b2" * 32
A_PUB, B_PUB = payer_pubkey_from_privkey(A_PRIV), payer_pubkey_from_privkey(B_PRIV)
AUTH = "06" * 32


class FakeBackend:
    def __init__(self):
        self.utxo = {}
    async def utxos(self, address):
        return self.utxo.get(address, [])
    async def daa_score(self):
        return 624_000_000


class FakeWallet:
    def __init__(self):
        self.boxes = {}   # (txid, payee, cents) -> daa ; None when spent
    def family(self):
        return Family(authority=AUTH, borrow_guard="00" * 32, ext="ac" * 32, network="testnet-10")
    def payee_xonly(self):
        return "re" * 32
    def verify(self, txid, to_xonly, cents, authority, since_daa):
        daa = self.boxes.get((txid, to_xonly, cents))
        if daa is None:
            return VerifyResult(ok=False, reason=f"payment {txid} not indexed yet at the expected box address", index=None, daa=None, retryable=True)
        return VerifyResult(ok=True, reason="", index=0, daa=daa, retryable=False)


def kusd_listing(priv, pub, cents, capability="summarize"):
    return Listing(capability=capability, endpoint="https://x/y", payee_pubkey=pub, price_usd=cents / 100, network="testnet-10",
                   currency="kUSD", price_cents=cents, schemes=["kusd-utxo"]).sign(priv)


def kas_listing(priv, pub, price=0.002):
    return Listing(capability="summarize", endpoint="https://x/y", payee_pubkey=pub, price_usd=price, network="testnet-10").sign(priv)


def _app(backend, wallet=None, oracle_price=None, fee_cents=0):
    return TestClient(create_registry_app(backend, network="testnet-10", db_path=tempfile.mktemp(suffix=".db"),
                                          kusd=wallet, kusd_listing_fee_cents=fee_cents, oracle_price=oracle_price))


def test_kusd_listing_round_trips_and_keeps_kas_signatures_unchanged():
    k = kusd_listing(A_PRIV, A_PUB, 5)
    d = k.to_dict()
    assert (d["currency"], d["price_cents"], d["price_usd"]) == ("kUSD", 5, 0.05)
    assert Listing.from_dict(d).verify()
    kas = kas_listing(B_PRIV, B_PUB)
    assert "currency" not in kas.to_dict() and "price_cents" not in kas.to_dict(), "KAS listings keep their canonical bytes"
    assert Listing.from_dict(kas.to_dict()).currency == "KAS"


def test_search_filters_by_currency():
    c = _app(FakeBackend(), FakeWallet())
    assert c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict()).status_code == 200
    assert c.post("/registry/list", json=kas_listing(B_PRIV, B_PUB).to_dict()).status_code == 200
    kusd = c.get("/registry/search", params={"currency": "kUSD"}).json()["providers"]
    assert [p["payee_pubkey"] for p in kusd] == [A_PUB] and kusd[0]["currency"] == "kUSD" and kusd[0]["price_cents"] == 5
    kas = c.get("/registry/search", params={"currency": "KAS"}).json()["providers"]
    assert [p["payee_pubkey"] for p in kas] == [B_PUB] and kas[0]["currency"] == "KAS"
    assert len(c.get("/registry/search").json()["providers"]) == 2


def test_settled_report_in_cents_verified_and_ranks_kusd():
    be, w = FakeBackend(), FakeWallet()
    c = _app(be, w)
    c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 1).to_dict())
    c.post("/registry/list", json=kusd_listing(B_PRIV, B_PUB, 9).to_dict())
    w.boxes[("d3" * 32, B_PUB, 9)] = 624_000_100
    got = c.post("/registry/settled", json={"payee_pubkey": B_PUB, "txid": "d3" * 32, "cents": 9}).json()
    assert got["ok"] and got["credited_cents"] == 9 and got["reputation"]["settled_cents"] == 9
    assert c.post("/registry/settled", json={"payee_pubkey": B_PUB, "txid": "d3" * 32, "cents": 9}).json().get("already_counted")
    prov = c.get("/registry/search", params={"currency": "kUSD"}).json()["providers"]
    assert [p["payee_pubkey"] for p in prov] == [B_PUB, A_PUB], "settled cents rank first, price second"


def test_settled_report_after_spend_refused():
    be, w = FakeBackend(), FakeWallet()
    c = _app(be, w)
    c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict())
    r = c.post("/registry/settled", json={"payee_pubkey": A_PUB, "txid": "d4" * 32, "cents": 5})
    assert r.status_code == 409
    assert "report before spending" in r.json()["error"]


def test_mixed_ranking_uses_usd_value_at_oracle_price():
    # KAS provider settled 5 KAS at $0.0268 = $0.134; kUSD provider settled 20 cents = $0.20: kUSD ranks first
    k_kas = rank_key({"reputation": {"settled_kas": 5.0, "settled_cents": 0}, "price_usd": 0.001, "listed_at": 1}, None, 268)
    k_kusd = rank_key({"reputation": {"settled_kas": 0.0, "settled_cents": 20}, "price_usd": 0.05, "listed_at": 1}, None, 268)
    assert k_kusd < k_kas, "sorted ascending: the larger USD volume sorts first"
    # per-currency searches ignore the other currency's volume
    assert rank_key({"reputation": {"settled_kas": 5.0, "settled_cents": 0}, "price_usd": 0.001, "listed_at": 1}, "kUSD", 268)[0] == 0
    assert rank_key({"reputation": {"settled_kas": 0.0, "settled_cents": 20}, "price_usd": 0.05, "listed_at": 1}, "KAS", 268)[0] == 0


def test_kusd_listing_fee_required():
    from k402 import K402
    from k402.addresses import CallbackAddressProvider
    be, w = FakeBackend(), FakeWallet()

    class Chain(FakeBackend):
        async def address_received_sompi(self, address):
            return 0
    k402 = K402(address_provider=CallbackAddressProvider(lambda pid: f"kaspa:addr{pid[:6]}"), backend=Chain(),
                network="testnet-10", min_payable_sompi=0, kusd=w, kusd_price_cents=10)
    app = create_registry_app(be, network="testnet-10", db_path=tempfile.mktemp(suffix=".db"), kusd=w, kusd_listing_fee_cents=10, k402=k402)
    k402.install(app)
    c = TestClient(app)
    r = c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict())
    assert r.status_code == 402
    offer = next(o for o in r.json()["accepts"] if o["scheme"] == "kusd-utxo")
    assert offer["amount_cents"] == "10"
    w.boxes[("d5" * 32, w.payee_xonly(), 10)] = 624_000_100
    paid = c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict(), headers={"X-K402-Payment": f"kusd-utxo {'d5' * 32} {offer['payment_id']}"})
    assert paid.status_code == 200 and paid.json()["ok"]
    # KAS listings stay free here
    assert c.post("/registry/list", json=kas_listing(B_PRIV, B_PUB).to_dict()).status_code == 200


def test_kusd_listing_fee_paid_on_the_x402_wire():
    from k402 import K402, x402
    from k402.addresses import CallbackAddressProvider
    be, w = FakeBackend(), FakeWallet()

    class Chain(FakeBackend):
        async def address_received_sompi(self, address):
            return 0
    k402 = K402(address_provider=CallbackAddressProvider(lambda pid: f"kaspa:addr{pid[:6]}"), backend=Chain(),
                network="testnet-10", min_payable_sompi=0, kusd=w, kusd_price_cents=10)
    app = create_registry_app(be, network="testnet-10", db_path=tempfile.mktemp(suffix=".db"), kusd=w, kusd_listing_fee_cents=10, k402=k402)
    k402.install(app)
    c = TestClient(app)
    r = c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict())
    assert r.status_code == 402
    req = next(a for a in x402.decode_required_header(r.headers[x402.HDR_REQUIRED])["accepts"] if x402.is_kusd(a))
    assert req["amount"] == "10" and req["payTo"] == w.payee_xonly()
    w.boxes[("d6" * 32, w.payee_xonly(), 10)] = 624_000_100
    pp = x402.payment_payload(accepted=req, txid="d6" * 32, payer=A_PUB)
    paid = c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict(), headers={x402.HDR_SIGNATURE: x402.encode_signature_header(pp)})
    assert paid.status_code == 200 and paid.json()["ok"], paid.text
