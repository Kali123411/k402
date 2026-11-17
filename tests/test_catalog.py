"""One registry for every listing class: signed (kUSD/KAS with a payee key) and attested (an x402 endpoint the
registry itself probed). The probe is the gate — nothing is listed that did not answer 402 with a valid offer —
and the registry re-probes, so a dead backend stops being offered to agents."""
import base64
import json
import tempfile

import httpx
import pytest
from fastapi.testclient import TestClient

from k402 import catalog
from k402.channel import payer_pubkey_from_privkey
from k402.registry import Listing
from k402.registry_server import create_registry_app

A_PRIV = "a1" * 32
A_PUB = payer_pubkey_from_privkey(A_PRIV)
PAY_TO = "kaspa:qze7l36kx4h926sfv00p8tj8gf7ulxmga6rc8nsn7dhnmppt6q8nyg5sr0xjy"


def x402_header(accepts):
    return base64.b64encode(json.dumps({"x402Version": 2, "resource": {"url": "https://svc.test/exact"}, "accepts": accepts}).encode()).decode()


def exact(amount="100000000", network="kaspa:mainnet", pay_to=PAY_TO, asset="KAS", extra=None):
    return {"scheme": "exact", "network": network, "amount": amount, "asset": asset, "payTo": pay_to,
            "maxTimeoutSeconds": 60, "extra": extra or {"binding": "kaspa-exact-v2", "profile": "standard-native"}}


class World:
    """Fake internet: endpoint -> (status, headers, body); `calls` counts probes per endpoint."""
    def __init__(self):
        self.sites = {}
        self.calls = {}

    def set(self, url, status=402, accepts=None, body=None, headers=None):
        h = dict(headers or {})
        if accepts is not None:
            h["payment-required"] = x402_header(accepts)
        self.sites[url] = (status, h, body)

    def transport(self):
        def handle(request: httpx.Request):
            url = str(request.url)
            self.calls[url] = self.calls.get(url, 0) + 1
            if url not in self.sites:
                raise httpx.ConnectError("no route to host")
            status, headers, body = self.sites[url]
            if isinstance(body, dict):
                return httpx.Response(status, headers=headers, json=body)
            return httpx.Response(status, headers=headers, content=body or b"")
        return httpx.MockTransport(handle)

    def client(self):
        return httpx.AsyncClient(transport=self.transport())


def public_resolver(host):
    return ["93.184.216.34"]


# ---------- the probe ----------

def test_public_url_guard_rejects_private_hosts_and_other_schemes():
    for bad in ("http://127.0.0.1:8080/x", "http://10.0.0.1/x", "http://192.168.4.25/x", "http://[::1]/x", "http://localhost/x",
                "ftp://example.com/x", "not a url", "https://169.254.169.254/latest"):
        with pytest.raises(ValueError):
            catalog.assert_public_url(bad, resolver=public_resolver)
    assert catalog.assert_public_url("https://svc.test/exact", resolver=public_resolver) == "https://svc.test/exact"
    # a public name that resolves to a private address is still refused (DNS rebinding)
    with pytest.raises(ValueError):
        catalog.assert_public_url("https://evil.test/x", resolver=lambda h: ["10.1.1.1"])


@pytest.mark.asyncio
async def test_probe_reads_an_x402_offer_and_normalises_it():
    w = World()
    w.set("https://svc.test/exact", accepts=[exact()])
    att = await catalog.probe("https://svc.test/exact", w.client())
    assert att.ok and att.status == 402 and att.wire == "x402"
    r = att.requirements[0]
    assert (r.scheme, r.network, r.amount_sompi, r.pay_to, r.currency) == ("exact", "kaspa:mainnet", 100_000_000, PAY_TO, "KAS")
    assert r.binding == "kaspa-exact-v2"


@pytest.mark.asyncio
async def test_probe_recognises_a_kusd_requirement_by_its_asset():
    w = World()
    w.set("https://svc.test/sum", accepts=[exact(amount="5", network="kaspa:testnet-10", pay_to="ab" * 32, asset="06" * 32,
                                               extra={"binding": "kusd-holder-v1", "payment_id": "p_1"})])
    att = await catalog.probe("https://svc.test/sum", w.client(), kusd_authority="06" * 32)
    r = att.requirements[0]
    assert r.currency == "kUSD" and r.cents == 5 and r.amount_sompi is None


@pytest.mark.asyncio
async def test_probe_reads_a_k402_body_challenge_too():
    w = World()
    w.set("https://svc.test/k", body={"k402": 1, "accepts": [{"scheme": "kaspa-utxo", "network": "testnet-10", "amount_sompi": "60000000",
                                                              "pay_to": "kaspatest:qq", "payment_id": "p"}]})
    att = await catalog.probe("https://svc.test/k", w.client())
    assert att.ok and att.wire == "k402"
    assert att.requirements[0].network == "kaspa:testnet-10" and att.requirements[0].amount_sompi == 60_000_000


@pytest.mark.asyncio
async def test_probe_failures_are_named_not_raised():
    w = World()
    w.set("https://svc.test/ok200", status=200, body={"hello": 1})
    w.set("https://svc.test/bare402", status=402, body={"error": "pay"})
    w.set("https://svc.test/redirect", status=302, headers={"location": "https://elsewhere"})
    w.set("https://svc.test/garbage", status=402, headers={"payment-required": "!!!"})
    for url, word in (("https://svc.test/ok200", "402"), ("https://svc.test/bare402", "offer"), ("https://svc.test/redirect", "redirect"),
                      ("https://svc.test/garbage", "PAYMENT-REQUIRED"), ("https://down.test/x", "connect")):
        att = await catalog.probe(url, w.client())
        assert not att.ok and word.lower() in att.error.lower(), (url, att.error)


def test_reconcile_takes_the_live_offer_as_truth():
    att = catalog.Attestation(endpoint="https://svc.test/exact", ok=True, status=402, wire="x402", latency_ms=5, probed_at=1,
                              requirements=[catalog.Requirement("exact", "kaspa:mainnet", 100_000_000, PAY_TO, "KAS", "kaspa-exact-v2", "standard-native")])
    ok = catalog.reconcile({"scheme": "exact", "network": "kaspa:mainnet"}, att)
    assert ok["pay_to"] == PAY_TO and ok["amount_sompi"] == 100_000_000
    with pytest.raises(ValueError, match="does not offer"):
        catalog.reconcile({"scheme": "exact", "network": "kaspa:testnet-10"}, att)
    with pytest.raises(ValueError, match="payTo"):
        catalog.reconcile({"pay_to": "kaspa:other"}, att)
    with pytest.raises(ValueError, match="amount"):
        catalog.reconcile({"amount_sompi": 1}, att)
    # nothing declared: the first requirement is the listing
    assert catalog.reconcile({}, att)["scheme"] == "exact"


# ---------- the registry ----------

class FakeBackend:
    async def utxos(self, address):
        return []


def registry(world, **kw):
    app = create_registry_app(FakeBackend(), network="mainnet", db_path=tempfile.mktemp(suffix=".db"),
                              probe_http=world.client(), probe_resolver=public_resolver, **kw)
    return TestClient(app)


def test_submit_lists_only_what_the_probe_attests_and_the_catalog_merges_both_classes():
    w = World()
    w.set("https://svc.test/exact", accepts=[exact()])
    c = registry(w)
    # a signed kUSD/KAS listing by key, as today
    lst = Listing(capability="summarize", endpoint="https://x/y", payee_pubkey=A_PUB, price_usd=0.002, network="mainnet",
                  description="signed one").sign(A_PRIV)
    assert c.post("/registry/list", json=lst.to_dict()).status_code == 200
    # an unsigned submission: the registry probes it and stores what the endpoint actually offers
    r = c.post("/registry/submit", json={"resource": "https://svc.test/exact", "serviceName": "Finality proof", "capability": "kaspa-finality-proof",
                                         "description": "SP1 proof", "scheme": "exact", "network": "kaspa:mainnet", "payTo": PAY_TO,
                                         "amountSompi": 100000000, "tags": ["zk"]})
    assert r.status_code == 200, r.text
    got = r.json()["listing"]
    assert got["kind"] == "attested" and got["pay_to"] == PAY_TO and got["amount_sompi"] == 100_000_000 and got["verified"] is True
    cat = c.get("/registry/catalog").json()
    kinds = {x["kind"] for x in cat["listings"]}
    assert kinds == {"signed", "attested"} and cat["count"] == 2
    signed = next(x for x in cat["listings"] if x["kind"] == "signed")
    assert signed["payee_pubkey"] == A_PUB and signed["network"] == "kaspa:mainnet" and "reputation" in signed
    # by network
    assert c.get("/registry/catalog?network=kaspa:testnet-10").json()["count"] == 0
    # same endpoint again replaces, never duplicates
    r2 = c.post("/registry/submit", json={"resource": "https://svc.test/exact", "serviceName": "Finality proof v2"})
    assert r2.status_code == 200 and c.get("/registry/catalog").json()["count"] == 2
    assert next(x for x in c.get("/registry/catalog").json()["listings"] if x["kind"] == "attested")["serviceName"] == "Finality proof v2"


def test_submit_refuses_what_the_probe_does_not_attest():
    w = World()
    w.set("https://svc.test/ok200", status=200, body={})
    w.set("https://svc.test/dust", accepts=[exact(amount="1000")])
    w.set("https://svc.test/exact", accepts=[exact()])
    c = registry(w)
    assert c.post("/registry/submit", json={"resource": "http://127.0.0.1:8080/x", "serviceName": "n"}).status_code == 400
    assert c.post("/registry/submit", json={"resource": "https://svc.test/ok200", "serviceName": "n"}).status_code == 400
    assert c.post("/registry/submit", json={"resource": "https://down.test/x", "serviceName": "n"}).status_code == 400
    r = c.post("/registry/submit", json={"resource": "https://svc.test/dust", "serviceName": "n"})
    assert r.status_code == 400 and "minimum" in r.json()["error"]
    r = c.post("/registry/submit", json={"resource": "https://svc.test/exact", "serviceName": "n", "payTo": "kaspa:other"})
    assert r.status_code == 400 and "payTo" in r.json()["error"]
    assert c.post("/registry/submit", json={"resource": "https://svc.test/exact"}).status_code == 400   # a name is required
    assert c.get("/registry/catalog").json()["count"] == 0


def test_reprobe_retires_a_dead_endpoint_after_three_misses_and_revives_it():
    w = World()
    w.set("https://svc.test/exact", accepts=[exact()])
    c = registry(w)
    assert c.post("/registry/submit", json={"resource": "https://svc.test/exact", "serviceName": "n"}).status_code == 200
    del w.sites["https://svc.test/exact"]           # the backend dies
    for i in range(1, 4):
        s = c.post("/registry/reprobe").json()
        assert s["probed"] == 1
        live = c.get("/registry/catalog").json()["count"]
        assert live == (1 if i < 3 else 0), (i, live)
    dead = c.get("/registry/catalog?include_dead=1").json()["listings"][0]
    assert dead["live"] is False and dead["fails"] == 3 and "connect" in dead["last_error"].lower()
    w.set("https://svc.test/exact", accepts=[exact()])   # it comes back
    c.post("/registry/reprobe")
    back = c.get("/registry/catalog").json()["listings"][0]
    assert back["live"] is True and back["fails"] == 0


def test_reprobe_also_refreshes_the_attested_terms_when_the_price_changes():
    w = World()
    w.set("https://svc.test/exact", accepts=[exact()])
    c = registry(w)
    c.post("/registry/submit", json={"resource": "https://svc.test/exact", "serviceName": "n"})
    w.set("https://svc.test/exact", accepts=[exact(amount="200000000")])
    c.post("/registry/reprobe")
    assert c.get("/registry/catalog").json()["listings"][0]["amount_sompi"] == 200_000_000


def test_the_reprobe_timer_starts_with_the_app_and_sweeps(monkeypatch):
    import asyncio as _asyncio
    import k402.registry_server as rs
    w = World()
    w.set("https://svc.test/exact", accepts=[exact()])
    app = create_registry_app(FakeBackend(), network="mainnet", db_path=tempfile.mktemp(suffix=".db"), probe_http=w.client(),
                              probe_resolver=public_resolver, reprobe_seconds=1)
    real_sleep = _asyncio.sleep
    async def quick(sec):
        await real_sleep(0.01)
    monkeypatch.setattr(rs.asyncio, "sleep", quick)
    with TestClient(app) as c:        # the context manager runs the startup handlers
        assert c.post("/registry/submit", json={"resource": "https://svc.test/exact", "serviceName": "n"}).status_code == 200
        import time as _t
        deadline = _t.time() + 5
        while w.calls.get("https://svc.test/exact", 0) < 3 and _t.time() < deadline:
            _t.sleep(0.05)
    assert w.calls["https://svc.test/exact"] >= 3, "the timer re-probed on its own"
