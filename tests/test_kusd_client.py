# KusdPayer: the agent side of the kUSD market. x402 first (PAYMENT-REQUIRED -> wallet.send -> PAYMENT-SIGNATURE),
# k402's own header as the fallback, PSM top-up when short, the full outlay stated, fail-over across providers —
# but never after a box was built. No chain: a fake wallet records sends/buys; httpx.MockTransport plays
# registry and providers.
import httpx
import pytest

from k402 import x402
from k402.kusd import Family, KusdError
from k402.kusd_client import HOLDER_VALUE_SOMPI, SEND_FEE_SOMPI, KusdPayer, Outlay, PaymentSent
from k402.kusd_keys import derive_payee
from k402.schemes import KusdOffer, payment_required_body

AUTH = "06" * 32
PAYEE = "ab" * 32          # the provider's listed (master) key
PID = "p_1"
PAY_TO = derive_payee(PAYEE, PID)   # what the provider's 402 quotes for offer p_1
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


def kusd_offer(cents, pid=PID, pay_to=PAY_TO, network="testnet-10"):
    return KusdOffer(network=network, amount_cents=str(cents), pay_to_pubkey=pay_to, authority=AUTH, payment_id=pid, expires=2 ** 31)


def required_header(cents, pid=PID, authority=AUTH, pay_to=PAY_TO, network=x402.NETWORK_TESTNET):
    accepts = [x402.requirements(network=network, amount_sompi=3_000_000, pay_to="kaspatest:qq", payment_id=pid),
               x402.kusd_requirements(network=network, amount_cents=cents, pay_to_pubkey=pay_to, authority=authority, payment_id=pid)]
    return x402.encode_required_header(x402.payment_required(resource=x402.resource_info("https://p.test/sum"), accepts=accepts))


class Provider:
    """One provider on both wires. `x402=False` leaves the PAYMENT-REQUIRED header off (k402-only provider);
    `not_indexed_first` answers the first paid attempt with a retryable 402 (k402 body reason, or on the x402
    wire a PAYMENT-RESPONSE errorReason payment_not_found); `fail_after_payment` answers a paid request 500."""
    def __init__(self, cents=5, x402=True, authority=AUTH, pay_to=PAY_TO, network="testnet-10", not_indexed_first=False,
                 x402_style_retry=False, fail_after_payment=False):
        self.cents, self.x402, self.authority, self.pay_to, self.network = cents, x402, authority, pay_to, network
        self.not_indexed_first, self.x402_style_retry, self.fail_after_payment = not_indexed_first, x402_style_retry, fail_after_payment
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
            assert scheme == "kusd-utxo" and pid == PID
        if txid:
            self.paid.append((txid, "x402" if sig else "k402"))
            if self.fail_after_payment:
                return httpx.Response(500, json={"error": "model crashed"})
            if self.not_indexed_first and len(self.paid) == 1:
                if self.x402_style_retry:
                    sr = x402.settle_response(success=False, network=x402.NETWORK_TESTNET, error_reason="payment_not_found")
                    return httpx.Response(402, json={}, headers={x402.HDR_RESPONSE: x402.encode_response_header(sr)})
                body = payment_required_body([kusd_offer(self.cents)]); body["reason"] = "payment not indexed yet (retry shortly)"
                return httpx.Response(402, json=body)
            return httpx.Response(200, json={"ok": True, "summary": "…"})
        xnet = x402.NETWORK_TESTNET if self.network == "testnet-10" else x402.NETWORK_MAINNET
        body = payment_required_body([kusd_offer(self.cents, pay_to=self.pay_to, network=self.network)])
        headers = {x402.HDR_REQUIRED: required_header(self.cents, authority=self.authority, pay_to=self.pay_to, network=xnet)} if self.x402 else {}
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


P_DICT = {"endpoint": "https://p.test/sum", "payee_pubkey": PAYEE}


@pytest.mark.asyncio
async def test_payer_pays_x402_kusd_offer():
    w, p = FakeWallet(), Provider(cents=5)
    res = await payer(w, {"p.test": p}).pay(P_DICT, "/sum", {"text": "hi"})
    assert res.response.status_code == 200 and res.wire == "x402" and res.cents == 5
    assert w.sent == [(PAY_TO, 5)] and p.paid == [(res.txid, "x402")]


@pytest.mark.asyncio
async def test_payer_falls_back_to_k402_header():
    w, p = FakeWallet(), Provider(cents=5, x402=False)
    res = await payer(w, {"p.test": p}).pay(P_DICT, "/sum", {})
    assert res.response.status_code == 200 and res.wire == "k402" and p.paid == [(res.txid, "k402")]


@pytest.mark.asyncio
async def test_payer_tops_up_from_psm_when_short():
    w, p = FakeWallet(cents=2), Provider(cents=5)
    res = await payer(w, {"p.test": p}).pay(P_DICT, "/sum", {})
    assert w.bought == [5 - 2 + 50], "shortfall plus the 50-cent headroom, bought through the PSM"
    assert res.response.status_code == 200 and res.topped_up["cents"] == 53


@pytest.mark.asyncio
async def test_payer_reports_full_outlay():
    w, p = FakeWallet(), Provider(cents=5)
    res = await payer(w, {"p.test": p}).pay(P_DICT, "/sum", {})
    assert res.outlay == Outlay(price_cents=5, box_kas_sompi=HOLDER_VALUE_SOMPI, fee_sompi=SEND_FEE_SOMPI)
    assert HOLDER_VALUE_SOMPI == 150_000_000 and SEND_FEE_SOMPI == 10_000_000
    assert res.outlay.kas_sompi == 160_000_000


@pytest.mark.asyncio
async def test_payer_refuses_another_familys_kusd_before_sending():
    w, p = FakeWallet(), Provider(cents=5, authority="ee" * 32)
    with pytest.raises(KusdError, match="asset mismatch"):
        await payer(w, {"p.test": p}).pay(P_DICT, "/sum", {})
    assert w.sent == []


@pytest.mark.asyncio
async def test_payer_refuses_a_price_above_its_cap_before_sending():
    w, p = FakeWallet(), Provider(cents=500)
    with pytest.raises(KusdError, match="above the cap"):
        await payer(w, {"p.test": p}).pay(P_DICT, "/sum", {}, max_price_cents=10)
    assert w.sent == []


@pytest.mark.asyncio
async def test_payer_refuses_a_quote_not_paid_to_the_listed_providers_key():
    # a hijacked endpoint quoting its own key is refused: the quote's payTo must be the listed key tweaked by
    # the offer's payment_id
    from k402.channel import payer_pubkey_from_privkey
    w, p = FakeWallet(), Provider(cents=5, pay_to=derive_payee(payer_pubkey_from_privkey("cd" * 32), PID))
    with pytest.raises(KusdError, match="payee mismatch"):
        await payer(w, {"p.test": p}).pay(P_DICT, "/sum", {})
    assert w.sent == []
    # without a listed key (a bare endpoint) the payer has nothing to check against and pays the quote
    res = await payer(w, {"p.test": p}).pay({"endpoint": "https://p.test/sum"}, "/sum", {})
    assert res.response.status_code == 200


@pytest.mark.asyncio
async def test_payer_refuses_a_quote_on_another_network():
    w, p = FakeWallet(), Provider(cents=5, network="mainnet")
    with pytest.raises(KusdError, match="network"):
        await payer(w, {"p.test": p}).pay(P_DICT, "/sum", {})
    assert w.sent == []


@pytest.mark.asyncio
async def test_payer_retries_the_same_payment_until_indexed_never_paying_twice():
    for style in (False, True):   # k402 body reason / x402 PAYMENT-RESPONSE payment_not_found
        w, p = FakeWallet(), Provider(cents=5, not_indexed_first=True, x402_style_retry=style)
        res = await payer(w, {"p.test": p}).pay(P_DICT, "/sum", {})
        assert res.response.status_code == 200 and len(w.sent) == 1 and [t for t, _ in p.paid] == [res.txid, res.txid]
        assert res.attempts == 2


@pytest.mark.asyncio
async def test_pay_best_never_fails_over_after_a_box_was_built():
    w = FakeWallet()
    broken, other = Provider(cents=5, fail_after_payment=True), Provider(cents=5)
    reg = [listing(PAYEE, "https://broken.test/sum", 5, settled_cents=9), listing("cd" * 32, "https://other.test/sum", 5)]
    with pytest.raises(PaymentSent) as e:
        await payer(w, {"broken.test": broken, "other.test": other}, reg).pay_best("summarize", json_body={"text": "x"})
    assert e.value.txid == "01" * 32 and e.value.cents == 5 and e.value.status == 500
    assert w.sent == [(PAY_TO, 5)] and other.paid == [], "the second provider was never paid"


@pytest.mark.asyncio
async def test_pay_best_caps_at_the_listed_price():
    w = FakeWallet()
    greedy = Provider(cents=50)   # listed at 5, quotes 50
    reg = [listing(PAYEE, "https://greedy.test/sum", 5)]
    from k402.channel_client import RouteError
    with pytest.raises(RouteError) as e:
        await payer(w, {"greedy.test": greedy}, reg).pay_best("summarize", json_body={})
    assert "above the cap" in str(e.value) and w.sent == []


@pytest.mark.asyncio
async def test_pay_best_fails_over_and_ranks_by_settled_cents():
    w = FakeWallet()
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
    assert route.result.cents == 5 and w.sent == [(PAY_TO, 5)]
