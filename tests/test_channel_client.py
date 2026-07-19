# Payer-side routing helpers. The full discover->open->pay->close cycle is proven live on TN10
# in the E-phase integration script; here we cover the pure logic (base-url derivation, discovery
# passthrough, and channel-reuse bookkeeping) without a node or covenant binary.
import pytest

from k402.channel import payer_pubkey_from_privkey
from k402.channel_client import ChannelPayer, _base_url

PAYER = "11" * 32


def test_base_url_from_capability_endpoint():
    assert _base_url("https://prov.example/v1/summarize") == "https://prov.example"
    assert _base_url("http://1.2.3.4:8402/work?x=1") == "http://1.2.3.4:8402"


class _Resp:
    def __init__(self, payload):
        self._payload = payload
    def json(self):
        return self._payload


class FakeRegistryHTTP:
    """Minimal stand-in for httpx used by RegistryClient.search."""
    def __init__(self, providers):
        self._providers = providers
    async def get(self, url, params=None):
        return _Resp({"providers": self._providers, "count": len(self._providers)})


@pytest.mark.asyncio
async def test_discover_passthrough_and_ranking_preserved():
    ranked = [{"capability": "summarize", "endpoint": "https://a/x", "payee_pubkey": "aa" * 32,
               "price_usd": 0.005, "channel_terms": {}, "reputation": {"settled_kas": 9.0}},
              {"capability": "summarize", "endpoint": "https://b/x", "payee_pubkey": "bb" * 32,
               "price_usd": 0.001, "channel_terms": {}, "reputation": {"settled_kas": 0.0}}]
    payer = ChannelPayer(PAYER, opener=None, backend=None, registry_url="https://reg")
    payer._registry._http = FakeRegistryHTTP(ranked)
    out = await payer.discover("summarize")
    # the client trusts the registry's ranking (reputation first) — order preserved
    assert [p["payee_pubkey"] for p in out] == ["aa" * 32, "bb" * 32]


@pytest.mark.asyncio
async def test_no_registry_raises():
    payer = ChannelPayer(PAYER, opener=None, backend=None)
    with pytest.raises(RuntimeError, match="no registry"):
        await payer.discover("summarize")


# ---------------------------------------------------------------- auto-routing (pay_best)
from k402.channel_client import RouteError  # noqa: E402


class _Resp2:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code, self._payload, self.text = status_code, (payload or {}), text
    def json(self):
        return self._payload


class FakeOpener:
    """Never touches a chain — returns a synthetic (channel_id, open_txid). channel_id must be hex
    (32-byte covenant id) so sign_voucher can parse it; the payee pubkey is a convenient valid hex."""
    def open(self, *, payer_privkey, payee_pubkey, expiry_daa, amount_sompi, maxfee):
        return payee_pubkey, "ff" * 32


class FakeBackend:
    async def daa_score(self):
        return 1000


class FakeProviderHTTP:
    """Routes by URL + headers: /channel/open -> 200; a call with X-K402-Payment -> the endpoint's
    scripted paid status; otherwise it's a preflight probe -> the endpoint's scripted preflight."""
    def __init__(self, endpoints):
        self.endpoints = endpoints          # base_url -> {"preflight": 402|500, "paid": 200|500, "body": {...}}
        self.open_calls, self.paid_calls, self.preflight_calls = [], [], []
    async def post(self, url, json=None, headers=None):
        base = _base_url(url)
        cfg = self.endpoints[base]
        if url.endswith("/channel/open"):
            self.open_calls.append(base)
            return _Resp2(200, {"ok": True})
        if headers and "X-K402-Payment" in headers:
            self.paid_calls.append(base)
            return _Resp2(cfg["paid"], cfg.get("body", {"ok": True}))
        self.preflight_calls.append(base)
        st = cfg.get("preflight", 402)
        return _Resp2(st, {"k402": "0.2", "accepts": []} if st == 402 else {})


def _prov(tag, price, rep=0.0):
    return {"capability": "summarize", "endpoint": f"https://{tag}/summarize",
            "payee_pubkey": (tag * 32)[:64], "price_usd": price, "channel_terms": {},
            "reputation": {"settled_kas": rep}}


def _payer(providers, http):
    payer = ChannelPayer(PAYER, opener=FakeOpener(), backend=FakeBackend(), registry_url="https://reg")
    payer._registry._http = FakeRegistryHTTP(providers)
    payer.http = http
    return payer


@pytest.mark.asyncio
async def test_pay_best_fails_over_to_next_provider():
    a, b = _prov("aa", 0.005, rep=9.0), _prov("bb", 0.001, rep=0.0)  # registry ranks A first
    http = FakeProviderHTTP({"https://aa": {"preflight": 402, "paid": 500},
                             "https://bb": {"preflight": 402, "paid": 200, "body": {"ok": True}}})
    res = await _payer([a, b], http).pay_best("summarize")
    assert res.response.status_code == 200
    assert res.provider["endpoint"] == "https://bb/summarize"
    assert http.paid_calls == ["https://aa", "https://bb"]           # tried A, then B
    assert [x[1] for x in res.attempts] == ["HTTP 500"]              # A's failure recorded


@pytest.mark.asyncio
async def test_pay_best_preflight_skips_dead_without_opening_channel():
    a, b = _prov("aa", 0.001), _prov("bb", 0.009)
    http = FakeProviderHTTP({"https://aa": {"preflight": 500, "paid": 200},   # A is dead
                             "https://bb": {"preflight": 402, "paid": 200, "body": {"ok": True}}})
    res = await _payer([a, b], http).pay_best("summarize")
    assert res.provider["endpoint"] == "https://bb/summarize"
    assert "https://aa" not in http.open_calls and "https://aa" not in http.paid_calls  # never opened a channel to the dead one


@pytest.mark.asyncio
async def test_pay_best_cheapest_policy_orders_by_price():
    a, b = _prov("aa", 0.005, rep=9.0), _prov("bb", 0.001, rep=0.0)  # registry order A,B; cheapest is B
    http = FakeProviderHTTP({"https://aa": {"preflight": 402, "paid": 200},
                             "https://bb": {"preflight": 402, "paid": 200, "body": {"ok": True}}})
    res = await _payer([a, b], http).pay_best("summarize", policy="cheapest")
    assert res.provider["endpoint"] == "https://bb/summarize"
    assert http.paid_calls == ["https://bb"]                         # went straight to the cheapest


@pytest.mark.asyncio
async def test_pay_best_all_fail_raises_routeerror():
    a, b = _prov("aa", 0.001), _prov("bb", 0.002)
    http = FakeProviderHTTP({"https://aa": {"preflight": 402, "paid": 500},
                             "https://bb": {"preflight": 402, "paid": 503}})
    with pytest.raises(RouteError, match="no provider for 'summarize'"):
        await _payer([a, b], http).pay_best("summarize")
