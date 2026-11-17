# The registry in kUSD: listings carry currency / price_cents without disturbing KAS signatures, search filters
# and ranks per currency (USD value at the oracle price when mixed), settled reports in cents are verified
# through the wallet at the per-offer payee key and refused once the box is spent, and listing in kUSD costs a
# kUSD fee — paid in kUSD only, and only consumed for a listing that is otherwise valid.
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

from k402 import K402, x402
from k402.addresses import CallbackAddressProvider
from k402.channel import payer_pubkey_from_privkey
from k402.kusd import Family, KusdError, VerifyResult
from k402.kusd_keys import derive_payee
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
    async def address_received_sompi(self, address):
        return 10 ** 12   # plenty of KAS everywhere: a KAS payment must never pass as a kUSD fee


class FakeWallet:
    def __init__(self):
        self.boxes = {}    # (txid, payee, cents) -> (daa, index); absent = not indexed / spent
        self.claims = []
        self.down = False
    def family(self):
        return Family(authority=AUTH, borrow_guard="00" * 32, ext="ac" * 32, network="testnet-10")
    def payee_xonly(self):
        return payer_pubkey_from_privkey("e1" * 32)
    def verify(self, txid, to_xonly, cents, authority, since_daa):
        if self.down:
            raise KusdError("node unreachable")
        hit = self.boxes.get((txid, to_xonly, cents))
        if hit is None:
            return VerifyResult(ok=False, reason=f"payment {txid} not indexed yet at the expected box address", index=None, daa=None, retryable=True)
        return VerifyResult(ok=True, reason="", index=hit[1], daa=hit[0], retryable=False)
    def claim(self, txid, index, cents, tweak):
        self.claims.append((txid, index, cents, tweak))
        return {"ok": True}


def kusd_listing(priv, pub, cents, capability="summarize", **over):
    kw = dict(capability=capability, endpoint="https://x/y", payee_pubkey=pub, price_usd=cents / 100, network="testnet-10",
              currency="kUSD", price_cents=cents, schemes=["kusd-utxo"])
    kw.update(over)
    return Listing(**kw).sign(priv)


def kas_listing(priv, pub, price=0.002):
    return Listing(capability="summarize", endpoint="https://x/y", payee_pubkey=pub, price_usd=price, network="testnet-10").sign(priv)


def _app(backend, wallet=None, oracle_price=268, fee_cents=0, k402=None, db=None):
    app = create_registry_app(backend, network="testnet-10", db_path=db or tempfile.mktemp(suffix=".db"),
                              kusd=wallet, kusd_listing_fee_cents=fee_cents, oracle_price=oracle_price, k402=k402)
    if k402 is not None:
        k402.install(app)
    return TestClient(app)


def gate(w, fee=10):
    return K402(address_provider=CallbackAddressProvider(lambda pid: f"kaspa:addr{pid[:6]}"), backend=FakeBackend(),
                network="testnet-10", min_payable_sompi=0, kusd=w, kusd_price_cents=fee)


def report(c, pub, txid, cents, pid):
    return c.post("/registry/settled", json={"payee_pubkey": pub, "txid": txid, "cents": cents, "payment_id": pid})


def test_kusd_listing_round_trips_and_keeps_kas_signatures_unchanged():
    k = kusd_listing(A_PRIV, A_PUB, 5)
    d = k.to_dict()
    assert (d["currency"], d["price_cents"], d["price_usd"]) == ("kUSD", 5, 0.05)
    assert Listing.from_dict(d).verify()
    kas = kas_listing(B_PRIV, B_PUB)
    assert "currency" not in kas.to_dict() and "price_cents" not in kas.to_dict(), "KAS listings keep their canonical bytes"
    assert Listing.from_dict(kas.to_dict()).currency == "KAS"
    # price_cents is outside a KAS listing's signed bytes, so it is never taken from the wire for one
    assert Listing.from_dict({**kas.to_dict(), "price_cents": 7}).price_cents == 0


def test_search_filters_by_currency():
    c = _app(FakeBackend(), FakeWallet())
    assert c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict()).status_code == 200
    assert c.post("/registry/list", json=kas_listing(B_PRIV, B_PUB).to_dict()).status_code == 200
    kusd = c.get("/registry/search", params={"currency": "kUSD"}).json()["providers"]
    assert [p["payee_pubkey"] for p in kusd] == [A_PUB] and kusd[0]["currency"] == "kUSD" and kusd[0]["price_cents"] == 5
    kas = c.get("/registry/search", params={"currency": "KAS"}).json()["providers"]
    assert [p["payee_pubkey"] for p in kas] == [B_PUB] and kas[0]["currency"] == "KAS"
    assert len(c.get("/registry/search").json()["providers"]) == 2


def test_kusd_listing_must_be_consistent():
    c = _app(FakeBackend(), FakeWallet())
    assert c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5, price_usd=0.0001).to_dict()).status_code == 400
    assert c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5, schemes=["kaspa-utxo"]).to_dict()).status_code == 400
    assert c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 0).to_dict()).status_code == 400


def test_settled_report_in_cents_verified_at_the_offers_key_and_ranks_kusd():
    be, w = FakeBackend(), FakeWallet()
    c = _app(be, w)
    c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 1).to_dict())
    c.post("/registry/list", json=kusd_listing(B_PRIV, B_PUB, 9).to_dict())
    w.boxes[("d3" * 32, derive_payee(B_PUB, "p_9"), 9)] = (624_000_100, 0)
    got = report(c, B_PUB, "d3" * 32, 9, "p_9").json()
    assert got["ok"] and got["credited_cents"] == 9 and got["reputation"]["settled_cents"] == 9
    assert report(c, B_PUB, "d3" * 32, 9, "p_9").json().get("already_counted")
    # a report at the bare listing key is not a payment (no offer pays there)
    assert report(c, B_PUB, "d3" * 32, 9, "").status_code == 400
    prov = c.get("/registry/search", params={"currency": "kUSD"}).json()["providers"]
    assert [p["payee_pubkey"] for p in prov] == [B_PUB, A_PUB], "settled cents rank first, price second"
    assert [p["payee_pubkey"] for p in c.get("/registry/search", params={"min_settled_cents": 5}).json()["providers"]] == [B_PUB]


def test_settled_reports_are_keyed_by_box_not_by_transaction():
    # one transaction creates the payee's box and the payer's change box; the payer reporting its change first
    # must not erase the payee's credit, and the same box is never credited twice
    be, w = FakeBackend(), FakeWallet()
    c = _app(be, w)
    c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict())
    c.post("/registry/list", json=kusd_listing(B_PRIV, B_PUB, 5).to_dict())
    w.boxes[("d4" * 32, derive_payee(A_PUB, "p_x"), 30)] = (624_000_100, 1)
    w.boxes[("d4" * 32, derive_payee(B_PUB, "p_5"), 5)] = (624_000_100, 0)
    assert report(c, A_PUB, "d4" * 32, 30, "p_x").json()["credited_cents"] == 30
    assert report(c, B_PUB, "d4" * 32, 5, "p_5").json()["credited_cents"] == 5
    assert report(c, B_PUB, "d4" * 32, 5, "p_5").json().get("already_counted")


def test_settled_report_after_spend_or_during_an_outage_is_not_credited():
    be, w = FakeBackend(), FakeWallet()
    c = _app(be, w)
    c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict())
    r = report(c, A_PUB, "d4" * 32, 5, "p_1")
    assert r.status_code == 503 and "report before spending" in r.json()["error"]
    w.down = True
    assert report(c, A_PUB, "d4" * 32, 5, "p_1").status_code == 502
    assert c.post("/registry/settled", json={"payee_pubkey": A_PUB, "txid": "d4" * 32, "cents": "five", "payment_id": "p"}).status_code == 400


def test_mixed_ranking_uses_usd_value_at_oracle_price():
    # KAS provider settled 5 KAS at $0.0268 = $0.134; kUSD provider settled 20 cents = $0.20: kUSD ranks first
    k_kas = rank_key({"reputation": {"settled_kas": 5.0, "settled_cents": 0}, "price_usd": 0.001, "listed_at": 1}, None, 268)
    k_kusd = rank_key({"reputation": {"settled_kas": 0.0, "settled_cents": 20}, "price_usd": 0.05, "listed_at": 1}, None, 268)
    assert k_kusd < k_kas, "sorted ascending: the larger USD volume sorts first"
    # per-currency searches ignore the other currency's volume
    assert rank_key({"reputation": {"settled_kas": 5.0, "settled_cents": 0}, "price_usd": 0.001, "listed_at": 1}, "kUSD", 268)[0] == 0
    assert rank_key({"reputation": {"settled_kas": 0.0, "settled_cents": 20}, "price_usd": 0.05, "listed_at": 1}, "KAS", 268)[0] == 0
    # a registry that lists kUSD must know the price of KAS, or the two volumes are not comparable
    with pytest.raises(ValueError, match="oracle_price"):
        create_registry_app(FakeBackend(), network="testnet-10", db_path=tempfile.mktemp(suffix=".db"), kusd=FakeWallet(), oracle_price=None)
    with pytest.raises(ValueError, match="k402"):
        create_registry_app(FakeBackend(), network="testnet-10", db_path=tempfile.mktemp(suffix=".db"), kusd=FakeWallet(), kusd_listing_fee_cents=10, oracle_price=268)


def test_kusd_listing_fee_is_paid_in_kusd_only_on_either_wire():
    w = FakeWallet()
    k402 = gate(w)
    c = _app(FakeBackend(), w, fee_cents=10, k402=k402)
    r = c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict())
    assert r.status_code == 402
    offer = next(o for o in r.json()["accepts"] if o["scheme"] == "kusd-utxo")
    kas = next(o for o in r.json()["accepts"] if o["scheme"] == "kaspa-utxo")
    assert offer["amount_cents"] == "10"
    # paying the KAS offer (the chain says the address received plenty) does not buy a kUSD listing
    paid_kas = c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict(),
                      headers={"X-K402-Payment": f"kaspa-utxo {'d0' * 32} {kas['payment_id']}"})
    assert paid_kas.status_code == 402 and "kUSD" in paid_kas.json()["reason"]
    # the kUSD offer, on the k402 wire
    w.boxes[("d5" * 32, offer["pay_to_pubkey"], 10)] = (624_000_100, 0)
    paid = c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict(), headers={"X-K402-Payment": f"kusd-utxo {'d5' * 32} {offer['payment_id']}"})
    assert paid.status_code == 200 and paid.json()["ok"]
    # and on the x402 wire
    r2 = c.post("/registry/list", json=kusd_listing(B_PRIV, B_PUB, 5).to_dict())
    req = next(a for a in x402.decode_required_header(r2.headers[x402.HDR_REQUIRED])["accepts"] if x402.is_kusd(a))
    assert req["amount"] == "10" and req["payTo"] == derive_payee(w.payee_xonly(), req["extra"]["payment_id"])
    w.boxes[("d6" * 32, req["payTo"], 10)] = (624_000_100, 0)
    pp = x402.payment_payload(accepted=req, txid="d6" * 32, payer=B_PUB)
    paid2 = c.post("/registry/list", json=kusd_listing(B_PRIV, B_PUB, 5).to_dict(), headers={x402.HDR_SIGNATURE: x402.encode_signature_header(pp)})
    assert paid2.status_code == 200 and paid2.json()["ok"], paid2.text
    # KAS listings stay free here
    assert c.post("/registry/list", json=kas_listing(B_PRIV, B_PUB).to_dict()).status_code == 200


def test_listing_fee_is_not_consumed_for_an_invalid_listing():
    w = FakeWallet()
    c = _app(FakeBackend(), w, fee_cents=10, k402=gate(w))
    offer = next(o for o in c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict()).json()["accepts"] if o["scheme"] == "kusd-utxo")
    w.boxes[("d7" * 32, offer["pay_to_pubkey"], 10)] = (624_000_100, 0)
    hdr = {"X-K402-Payment": f"kusd-utxo {'d7' * 32} {offer['payment_id']}"}
    bad = kusd_listing(A_PRIV, A_PUB, 5).to_dict()
    bad["sig"] = "00" * 64
    assert c.post("/registry/list", json=bad, headers=hdr).status_code == 400
    assert c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict(), headers=hdr).status_code == 200, "the fee still buys the corrected listing"


def test_registry_migrates_a_database_from_before_currencies():
    db = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE listings (payee_pubkey TEXT NOT NULL, capability TEXT NOT NULL, endpoint TEXT NOT NULL,
        price_usd REAL NOT NULL, network TEXT NOT NULL, schemes TEXT NOT NULL, channel_terms TEXT NOT NULL, stake_outpoint TEXT,
        stake_sompi INTEGER DEFAULT 0, meta TEXT NOT NULL, listed_at INTEGER NOT NULL, sig TEXT NOT NULL, PRIMARY KEY (payee_pubkey, capability))""")
    con.execute("CREATE TABLE reputation (payee_pubkey TEXT PRIMARY KEY, settled_sompi INTEGER NOT NULL DEFAULT 0, close_count INTEGER NOT NULL DEFAULT 0, first_settled INTEGER, last_settled INTEGER)")
    con.execute("INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (B_PUB, "summarize", "https://old", 0.002, "testnet-10", "[]", "{}", None, 0, "{}", 1, "sig"))
    con.commit(); con.close()
    c = _app(FakeBackend(), FakeWallet(), db=db)
    assert c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict()).status_code == 200
    rows = {p["payee_pubkey"]: p for p in c.get("/registry/search").json()["providers"]}
    assert rows[B_PUB]["currency"] == "KAS" and rows[B_PUB]["price_cents"] == 0 and rows[A_PUB]["currency"] == "kUSD"


# ------------------------------------------------------------------ the market page's feed + browser access
def test_activity_lists_verified_settlements_newest_first():
    be, w = FakeBackend(), FakeWallet()
    c = _app(be, w)
    c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict())
    c.post("/registry/list", json=kusd_listing(B_PRIV, B_PUB, 9, capability="extract").to_dict())
    w.boxes[("d3" * 32, derive_payee(A_PUB, "p_1"), 5)] = (624_000_100, 0)
    w.boxes[("d4" * 32, derive_payee(B_PUB, "p_2"), 9)] = (624_000_200, 1)
    assert report(c, A_PUB, "d3" * 32, 5, "p_1").json()["ok"]
    assert report(c, B_PUB, "d4" * 32, 9, "p_2").json()["ok"]
    got = c.get("/registry/activity").json()
    assert got["total_cents"] == 14 and [s["txid"] for s in got["settlements"]] == ["d4" * 32, "d3" * 32], "newest first"
    s = got["settlements"][0]
    assert s == {"t": s["t"], "txid": "d4" * 32, "idx": 1, "payee_pubkey": B_PUB, "cents": 9, "capability": "extract"}
    assert isinstance(s["t"], int) and s["t"] > 1_700_000_000
    assert len(c.get("/registry/activity", params={"limit": 1}).json()["settlements"]) == 1
    assert c.get("/registry/activity", params={"limit": 999}).json()["settlements"] == got["settlements"], "limit is clamped, never an error"
    assert c.get("/registry/activity", params={"limit": -3}).status_code == 200
    # a provider with several kUSD listings: the report names the capability that was paid (checked against its
    # listings; an unknown name falls back to its oldest listing), so the feed says what agents paid FOR
    c.post("/registry/list", json=kusd_listing(B_PRIV, B_PUB, 7, capability="summarize").to_dict())
    w.boxes[("d5" * 32, derive_payee(B_PUB, "p_3"), 7)] = (624_000_300, 0)
    w.boxes[("d6" * 32, derive_payee(B_PUB, "p_4"), 7)] = (624_000_400, 0)
    assert c.post("/registry/settled", json={"payee_pubkey": B_PUB, "txid": "d5" * 32, "cents": 7, "payment_id": "p_3", "capability": "summarize"}).json()["ok"]
    assert c.post("/registry/settled", json={"payee_pubkey": B_PUB, "txid": "d6" * 32, "cents": 7, "payment_id": "p_4", "capability": "not-listed"}).json()["ok"]
    caps = {s["txid"][:2]: s["capability"] for s in c.get("/registry/activity").json()["settlements"]}
    assert caps == {"d6": "extract", "d5": "summarize", "d4": "extract", "d3": "summarize"}
    # a delisted provider's settlements stay in the feed; what it reported stands, the rest has no capability to show
    for cap in ("extract", "summarize"):
        assert c.request("DELETE", "/registry/list", json=kusd_listing(B_PRIV, B_PUB, 9 if cap == "extract" else 7, capability=cap).to_dict()).status_code == 200
    caps = {s["txid"][:2]: s["capability"] for s in c.get("/registry/activity").json()["settlements"]}
    assert caps == {"d6": "", "d5": "summarize", "d4": "", "d3": "summarize"}


def test_cors_preflight_allows_x402_headers():
    app = create_registry_app(FakeBackend(), network="testnet-10", db_path=tempfile.mktemp(suffix=".db"), kusd=FakeWallet(),
                              oracle_price=268, cors_origins=["https://kaspa-app.vercel.app", "http://localhost:3000"])
    c = TestClient(app)
    r = c.options("/registry/search", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET",
                                                "Access-Control-Request-Headers": "payment-signature,content-type"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "payment-signature" in r.headers["access-control-allow-headers"].lower()
    r = c.get("/registry/search", headers={"Origin": "https://kaspa-app.vercel.app"})
    assert r.headers["access-control-allow-origin"] == "https://kaspa-app.vercel.app"
    assert "payment-required" in r.headers["access-control-expose-headers"].lower()
    # an origin the operator did not name gets nothing
    r = c.get("/registry/search", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    # the same helper on a provider's own app (what the browser agent talks to)
    from fastapi import FastAPI
    from k402 import allow_cors
    p = FastAPI()
    allow_cors(p, ["http://localhost:3000"])

    @p.post("/summarize")
    def summarize():
        return {"ok": True}
    pc = TestClient(p)
    r = pc.options("/summarize", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST",
                                          "Access-Control-Request-Headers": "payment-signature,x-k402-payment,content-type"})
    assert r.status_code == 200 and "x-k402-payment" in r.headers["access-control-allow-headers"].lower()
    r = pc.post("/summarize", json={}, headers={"Origin": "http://localhost:3000"})
    assert "payment-response" in r.headers["access-control-expose-headers"].lower()
    # no origins named → the helper is a no-op; a wildcard is refused (a provider must name who may pay from a page)
    q = FastAPI()
    allow_cors(q, [])
    assert not q.user_middleware
    with pytest.raises(ValueError, match="wildcard"):
        allow_cors(FastAPI(), ["*"])
    from k402.cors import origins_from_env
    assert origins_from_env(" https://a.example, http://localhost:3000 ,") == ["https://a.example", "http://localhost:3000"]


def test_registry_without_cors_sends_no_cors_headers():
    c = _app(FakeBackend(), FakeWallet())
    r = c.get("/registry/search", headers={"Origin": "http://localhost:3000"})
    assert "access-control-allow-origin" not in r.headers


def test_reputation_is_derived_from_the_verified_settlement_rows():
    # the live registry drifted: a counter said 55¢ over 11 reports while the verified rows summed 50¢ over 10
    # (one credit from before the dedup table existed). The rows are the truth: reputation reads them.
    be, w = FakeBackend(), FakeWallet()
    db = tempfile.mktemp(suffix=".db")
    c = _app(be, w, db=db)
    c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict())
    w.boxes[("d3" * 32, derive_payee(A_PUB, "p_1"), 5)] = (624_000_100, 0)
    w.boxes[("d4" * 32, derive_payee(A_PUB, "p_2"), 5)] = (624_000_200, 0)
    assert report(c, A_PUB, "d3" * 32, 5, "p_1").json()["ok"] and report(c, A_PUB, "d4" * 32, 5, "p_2").json()["ok"]
    # a phantom credit, as the old counter path could leave behind
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE reputation SET settled_cents=settled_cents+5, close_count=close_count+1 WHERE payee_pubkey=?", (A_PUB,))
    prov = c.get("/registry/provider/" + A_PUB).json()
    assert prov["reputation"]["settled_cents"] == 10, "the verified rows, not the counter"
    assert prov["reputation"]["kusd_payments"] == 2
    assert prov["reputation"]["closes"] == 0, "closes counts KAS channel closes only"
    assert prov["reputation"]["first_settled"] <= prov["reputation"]["last_settled"]
    # the feed and the listing now agree
    assert c.get("/registry/activity").json()["total_cents"] == 10
    assert c.get("/registry/search", params={"currency": "kUSD"}).json()["providers"][0]["reputation"]["settled_cents"] == 10


def test_settled_lookup_by_txid():
    be, w = FakeBackend(), FakeWallet()
    c = _app(be, w)
    c.post("/registry/list", json=kusd_listing(A_PRIV, A_PUB, 5).to_dict())
    w.boxes[("d3" * 32, derive_payee(A_PUB, "p_1"), 5)] = (624_000_100, 0)
    assert report(c, A_PUB, "d3" * 32, 5, "p_1").json()["ok"]
    got = c.get("/registry/settled/" + "D3" * 32).json()   # case-insensitive, canonical lowercase out
    assert got["txid"] == "d3" * 32 and got["verified"] is True
    assert got["settlements"] == [{"idx": 0, "payee_pubkey": A_PUB, "cents": 5, "t": got["settlements"][0]["t"], "capability": "summarize"}]
    assert c.get("/registry/settled/" + "ee" * 32).status_code == 404
    assert c.get("/registry/settled/not-a-txid").status_code == 400
