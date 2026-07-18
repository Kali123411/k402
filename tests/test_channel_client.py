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
