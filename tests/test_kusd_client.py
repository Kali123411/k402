# KusdPayer: the agent side of the kUSD market. x402 first (PAYMENT-REQUIRED -> wallet.send -> PAYMENT-SIGNATURE),
# k402's own header as the fallback, PSM top-up when short, the full outlay stated, fail-over across providers.
# No chain: a fake wallet records sends/buys; httpx.MockTransport plays registry and providers.
import json

import httpx
import pytest

from k402 import x402
from k402.kusd import Family, KusdError
from k402.kusd_client import KusdPayer, Outlay, HOLDER_VALUE_SOMPI, SEND_FEE_SOMPI
from k402.schemes import KusdOffer, payment_required_body

AUTH = "06" * 32
PAYEE = "ab" * 32
ME = "ea" * 32


class FakeWallet:
    def __init__(self, cents=100):
        self.cents = cents
        self.sent, self.bought = [], []
    def family(self):
        return Family(authority=AUTH, borrow_guard="00" * 32, ext="ac" * 32, network="testnet-10")
    def payee_xonly(self):
        return ME
    def balance(self):
        return {"kusd_cents": self.cents, "boxes": [], "kas_sompi": 10 ** 9, "xonly": ME}
    def send(self, to_xonly, cents):
        self.sent.append((to_xonly, cents))
        self.cents -= cents
        return {"txid": f"{len(self.sent):02x}" * 32, "index": 0, "amount": cents, "change_cents": self.cents}
    def buy(self, cents):
        self.bought.append(cents)
        self.cents += cents
        return {"txid": "bb" * 32, "cents": cents, "kas_in": cents * 37_000_000, "fee": 7_000_000}


def kusd_offer(cents, pid="p_1"):
    return KusdOffer(network="testnet-10", amount_cents=str(cents), pay_to_pubkey=PAYEE, authority=AUTH, payment_id=pid, expires=2 ** 31)


def required_header(cents, pid="p_1", authority=AUTH):
    accepts = [x402.requirements(network=x402.NETWORK_TESTNET, amount_sompi=3_000_000, pay_to="kaspatest:qq", payment_id=pid),
               x402.kusd_requirements(network=x402.NETWORK_TESTNET, amount_cents=cents, pay_to_pubkey=PAYEE, authority=authority, payment_id=pid)]
    return x402.encode_required_header(x402.payment_required(resource=x402.resource_info("https://p.test/sum"), accepts=accepts))


class Provider:
    """One provider on both wires. `x402=False` leaves the PAYMENT-REQUIRED header off (k402-only provider);
    `not_indexed_first` answers the first paid attempt with a retryable 402."""
    def __init__(self, cents=5, x402=True, authority=AUTH, not_indexed_first=False):
        self.cents, self.x402, self.authority, self.not_indexed_first = cents, x402, authority, not_indexed_first
        self.paid = []
    def __call__(self, request: httpx.Request) -> httpx.Response:
        sig = request.headers.get(x402.HDR_SIGNATURE)
        k402 = request.headers.get("X-K402-Payment")
        txid = None
        if sig:
            pp = x402.decode_signature_header(sig)
            assert x402.is_kusd(pp["accepted"]) and pp["accepted"]["asset"] == self.authority
            txid = pp["payload"]["transaction"]
        elif k402:
            scheme, txid, pid = k402.split()
            assert scheme == "kusd-utxo" and pid == "p_1"
        if txid:
            self.paid.append((txid, "x402" if sig else "k402"))
            if self.not_indexed_first and len(self.paid) == 1:
                body = payment_required_body([kusd_offer(self.cents)]); body["reason"] = "payment not indexed yet (retry shortly)"
                return httpx.Response(402, json=body)
            return httpx.Response(200, json={"ok": True, "summary": "…"})
        body = payment_required_body([kusd_offer(self.cents)])
        headers = {x402.HDR_REQUIRED: required_header(self.cents, authority=self.authority)} if self.x402 else {}
        return httpx.Response(402, json=body, headers=headers)


def listing(pub, endpoint, cents, settled_cents=0):
    return {"capability": "summarize", "endpoint": endpoint, "payee_pubkey": pub, "price_usd": cents / 100, "currency": "kUSD",
            "price_cents": cents, "network": "testnet-10", "schemes": ["kusd-utxo"], "listed_at": 1,
            "reputation": {"settled_kas": 0.0, "settled_cents": settled_cents}}


def mock(providers: dict, registry: list = ()):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "registry.test":
            assert request.url.params.get("currency") == "kUSD"
            return httpx.Response(200, json={"providers": list(registry), "count": len(registry)})
        return providers[request.url.host](request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def payer(wallet, providers, registry=()):
    return KusdPayer(wallet, registry_url="http://registry.test", network="testnet-10", http=mock(providers, registry), retry_delays=(0, 0))


@pytest.mark.asyncio
async def test_payer_pays_x402_kusd_offer():
    w, p = FakeWallet(), Provider(cents=5)
    res = await payer(w, {"p.test": p}).pay({"endpoint": "https://p.test/sum"}, "/sum", {"text": "hi"})
    assert res.response.status_code == 200 and res.wire == "x402" and res.cents == 5
    assert w.sent == [(PAYEE, 5)] and p.paid == [(res.txid, "x402")]


@pytest.mark.asyncio
async def test_payer_falls_back_to_k402_header():
    w, p = FakeWallet(), Provider(cents=5, x402=False)
    res = await payer(w, {"p.test": p}).pay({"endpoint": "https://p.test/sum"}, "/sum", {})
    assert res.response.status_code == 200 and res.wire == "k402" and p.paid == [(res.txid, "k402")]


@pytest.mark.asyncio
async def test_payer_tops_up_from_psm_when_short():
    w, p = FakeWallet(cents=2), Provider(cents=5)
    res = await payer(w, {"p.test": p}).pay({"endpoint": "https://p.test/sum"}, "/sum", {})
    assert w.bought == [5 - 2 + 50], "shortfall plus the 50-cent headroom, bought through the PSM"
    assert res.response.status_code == 200 and res.topped_up["cents"] == 53


@pytest.mark.asyncio
async def test_payer_reports_full_outlay():
    w, p = FakeWallet(), Provider(cents=5)
    res = await payer(w, {"p.test": p}).pay({"endpoint": "https://p.test/sum"}, "/sum", {})
    assert res.outlay == Outlay(price_cents=5, box_kas_sompi=HOLDER_VALUE_SOMPI, fee_sompi=SEND_FEE_SOMPI)
    assert HOLDER_VALUE_SOMPI == 150_000_000 and SEND_FEE_SOMPI == 10_000_000
    assert res.outlay.kas_sompi == 160_000_000


@pytest.mark.asyncio
async def test_payer_refuses_another_familys_kusd_before_sending():
    w, p = FakeWallet(), Provider(cents=5, authority="ee" * 32)
    with pytest.raises(KusdError, match="asset mismatch"):
        await payer(w, {"p.test": p}).pay({"endpoint": "https://p.test/sum"}, "/sum", {})
    assert w.sent == []


@pytest.mark.asyncio
async def test_payer_refuses_a_price_above_its_cap_before_sending():
    w, p = FakeWallet(), Provider(cents=500)
    with pytest.raises(KusdError, match="above the cap"):
        await payer(w, {"p.test": p}).pay({"endpoint": "https://p.test/sum"}, "/sum", {}, max_price_cents=10)
    assert w.sent == []


@pytest.mark.asyncio
async def test_payer_retries_the_same_payment_until_indexed_never_paying_twice():
    w, p = FakeWallet(), Provider(cents=5, not_indexed_first=True)
    res = await payer(w, {"p.test": p}).pay({"endpoint": "https://p.test/sum"}, "/sum", {})
    assert res.response.status_code == 200 and len(w.sent) == 1 and [t for t, _ in p.paid] == [res.txid, res.txid]


@pytest.mark.asyncio
async def test_pay_best_fails_over_and_ranks_by_settled_cents():
    w = FakeWallet()
    dead = Provider(cents=1)
    good = Provider(cents=5)
    def dead_handler(request):   # a listing whose endpoint is no longer a pay-gate
        return httpx.Response(200, json={"hello": "free"})
    reg = [listing("aa" * 32, "https://dead.test/sum", 1, settled_cents=900), listing(PAYEE, "https://good.test/sum", 5, settled_cents=10)]
    kp = payer(w, {"dead.test": dead_handler, "good.test": good}, reg)
    assert [p["payee_pubkey"] for p in KusdPayer.rank(reg, "cheapest")] == ["aa" * 32, PAYEE]
    assert [p["payee_pubkey"] for p in KusdPayer.rank(reg[::-1], "reputation")] == ["aa" * 32, PAYEE]
    route = await kp.pay_best("summarize", json_body={"text": "x"})
    assert route.provider["payee_pubkey"] == PAYEE and route.response.status_code == 200
    assert route.attempts == [("aa" * 32, "preflight: not a live kUSD pay-gate")]
    assert route.result.cents == 5 and w.sent == [(PAYEE, 5)]
