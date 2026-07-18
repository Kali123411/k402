# Registry: signed listings (canonical BIP340), the service's list/search/reputation flow with a
# fake chain backend, and rank-by-reputation. No node, no covenant binary.
import tempfile

import pytest
from fastapi.testclient import TestClient

from k402.channel import payer_pubkey_from_privkey
from k402.registry import Listing
from k402.registry_server import create_registry_app

A_PRIV, B_PRIV = "a1" * 32, "b2" * 32
A_PUB, B_PUB = payer_pubkey_from_privkey(A_PRIV), payer_pubkey_from_privkey(B_PRIV)


def a_listing(priv, pub, capability="summarize", price=0.002):
    return Listing(capability=capability, endpoint="https://x/y", payee_pubkey=pub,
                   price_usd=price, network="mainnet",
                   channel_terms={"min_sompi": 100_000_000, "max_sompi": 500_000_000},
                   meta={"model": "test"}).sign(priv)


class FakeBackend:
    """Returns a crafted close-output UTXO for reputation verification, keyed by (address, txid)."""
    def __init__(self):
        self.utxo = {}  # address -> list of {outpoint, utxoEntry}
    async def utxos(self, address):
        return self.utxo.get(address, [])


def _app(backend):
    return TestClient(create_registry_app(backend, network="mainnet",
                                          db_path=tempfile.mktemp(suffix=".db")))


def test_listing_sign_verify_roundtrip():
    lst = a_listing(A_PRIV, A_PUB)
    assert lst.verify()
    assert Listing.from_dict(lst.to_dict()).verify()
    # tamper: change the price after signing -> verify fails
    lst.price_usd = 0.0
    assert not lst.verify()


def test_description_signs_roundtrips_and_is_covered_by_signature():
    lst = Listing(capability="summarize", endpoint="https://x/y", payee_pubkey=A_PUB, price_usd=0.002,
                  network="mainnet", description="Fast 7B summariser, EU region.").sign(A_PRIV)
    assert lst.verify()
    r = Listing.from_dict(lst.to_dict())
    assert r.description == "Fast 7B summariser, EU region." and r.verify()
    # description is signed: tampering with it breaks the signature
    r.description = "something else"
    assert not r.verify()
    # a description-less listing keeps the pre-field canonical (backward-compatible)
    assert "description" not in a_listing(A_PRIV, A_PUB).to_dict()


def test_wrong_key_signature_rejected():
    lst = Listing(capability="summarize", endpoint="https://x", payee_pubkey=A_PUB, price_usd=0.002)
    with pytest.raises(ValueError, match="does not match"):
        lst.sign(B_PRIV)  # signing key must match the declared payee


def test_list_and_search():
    c = _app(FakeBackend())
    assert c.post("/registry/list", json=a_listing(A_PRIV, A_PUB).to_dict()).json()["ok"]
    r = c.get("/registry/search", params={"capability": "summarize"}).json()
    assert r["count"] == 1 and r["providers"][0]["payee_pubkey"] == A_PUB


def test_unsigned_listing_rejected():
    c = _app(FakeBackend())
    bad = a_listing(A_PRIV, A_PUB).to_dict()
    bad["sig"] = "00" * 64
    assert c.post("/registry/list", json=bad).status_code == 400


def test_reputation_from_verified_close_and_ranking():
    PublicKey = pytest.importorskip("kaspa").PublicKey  # reputation verify derives the payee address
    be = FakeBackend()
    c = _app(be)
    # two providers, same capability; B will out-earn A on-chain and should rank first
    c.post("/registry/list", json=a_listing(A_PRIV, A_PUB, price=0.001).to_dict())
    c.post("/registry/list", json=a_listing(B_PRIV, B_PUB, price=0.005).to_dict())
    b_addr = str(PublicKey(B_PUB).to_address("mainnet"))
    be.utxo[b_addr] = [{"outpoint": {"transactionId": "ff" * 32, "index": 0},
                        "utxoEntry": {"amount": 500_000_000}}]  # B closed a 5 KAS settlement
    got = c.post("/registry/settled", json={"payee_pubkey": B_PUB, "close_txid": "ff" * 32}).json()
    assert got["ok"] and got["credited_sompi"] == 500_000_000
    # replay is idempotent (deduped by txid)
    assert c.post("/registry/settled", json={"payee_pubkey": B_PUB, "close_txid": "ff" * 32}).json().get("already_counted")
    # search now ranks B first despite its higher price, because it has settled volume
    prov = c.get("/registry/search", params={"capability": "summarize"}).json()["providers"]
    assert prov[0]["payee_pubkey"] == B_PUB and prov[0]["reputation"]["settled_kas"] == 5.0
    # min_reputation filter drops the zero-rep provider
    filtered = c.get("/registry/search", params={"min_reputation_kas": 1.0}).json()
    assert [p["payee_pubkey"] for p in filtered["providers"]] == [B_PUB]


def test_unverified_close_rejected():
    pytest.importorskip("kaspa")  # settle path derives the payee address via the kaspa SDK
    c = _app(FakeBackend())  # no utxo -> close doesn't pay the payee
    c.post("/registry/list", json=a_listing(A_PRIV, A_PUB).to_dict())
    assert c.post("/registry/settled", json={"payee_pubkey": A_PUB, "close_txid": "ab" * 32}).status_code == 400


def test_signed_delist():
    c = _app(FakeBackend())
    lst = a_listing(A_PRIV, A_PUB)
    c.post("/registry/list", json=lst.to_dict())
    assert c.request("DELETE", "/registry/list", json=lst.to_dict()).json()["ok"]
    assert c.get("/registry/search").json()["count"] == 0
