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
from k402.channel import payer_pubkey_from_privkey
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
                 x402_style_retry=False, fail_after_payment=False, timeout_after_payment=False):
        self.cents, self.x402, self.authority, self.pay_to, self.network = cents, x402, authority, pay_to, network
        self.not_indexed_first, self.x402_style_retry, self.fail_after_payment = not_indexed_first, x402_style_retry, fail_after_payment
        self.timeout_after_payment = timeout_after_payment   # the paid request never gets an answer (transport error)
        self.paid, self.hits = [], 0
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hits += 1
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
            if self.timeout_after_payment:
                raise httpx.ReadTimeout("the provider took the box and went quiet", request=request)
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
async def test_payer_treats_a_listing_with_a_garbage_key_as_a_refusal_not_a_crash():
    # "cd"*32 is not on the curve: the registry (or a hand-typed dict) handed us a key no quote can match
    w, p = FakeWallet(), Provider(cents=5)
    with pytest.raises(KusdError, match="not a valid x-only"):
        await payer(w, {"p.test": p}).pay({"endpoint": "https://p.test/sum", "payee_pubkey": "cd" * 32}, "/sum", {})
    assert w.sent == []
    from k402.channel_client import RouteError
    with pytest.raises(RouteError):   # and pay_best fails over past it instead of dying on a ValueError
        await payer(w, {"p.test": p}, [listing("cd" * 32, "https://p.test/sum", 5)]).pay_best("summarize", json_body={})


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


# ------------------------------------------------------------------ operator spending controls (DESIGN-kusd-spend-controls.md)
from k402.spend import SpendLedger, SpendPolicy, SpendRefused  # noqa: E402


def bounded(wallet, providers, registry=(), **policy):
    led = SpendLedger(":memory:")
    kp = KusdPayer(wallet, registry_url="http://registry.test", network="testnet-10", http=mock(providers, registry), retry_delays=(0, 0),
                   policy=SpendPolicy(**policy), ledger=led)
    return kp, led


def test_payer_policy_requires_a_ledger():
    with pytest.raises(ValueError, match="ledger"):
        KusdPayer(FakeWallet(), policy=SpendPolicy(max_total_cents=1))
    assert payer(FakeWallet(), {}).spend_status() is None, "no policy: nothing to report, nothing changes"


@pytest.mark.asyncio
async def test_policy_cap_overrides_a_looser_agent_cap():
    w, p = FakeWallet(), Provider(cents=20)
    kp, _ = bounded(w, {"p.test": p}, max_per_call_cents=10)
    with pytest.raises(SpendRefused) as e:
        await kp.pay(P_DICT, "/sum", {}, max_price_cents=50)   # the agent asked for more than the operator allows
    assert e.value.code == "per_call" and "10" in str(e.value)
    kp8, _ = bounded(w, {"p.test": Provider(cents=8)}, max_per_call_cents=10)   # inside the operator's ceiling…
    with pytest.raises(KusdError, match="above the cap of 3"):
        await kp8.pay(P_DICT, "/sum", {}, max_price_cents=3)   # …but the agent's tighter cap still counts
    assert w.sent == [] and kp.spend_status()["total_cents"] == 0


@pytest.mark.asyncio
async def test_pay_best_discovery_honours_the_policy_ceiling():
    # a listing above the operator's per-call ceiling is not even tried (it would only burn max_tries)
    w, p = FakeWallet(), Provider(cents=50)
    kp, _ = bounded(w, {"p.test": p}, [listing(PAYEE, "https://p.test/sum", 50)], max_per_call_cents=10)
    from k402.channel_client import RouteError
    with pytest.raises(RouteError):
        await kp.pay_best("summarize", json_body={})
    assert p.hits == 0 and w.sent == []


@pytest.mark.asyncio
async def test_policy_budget_refuses_before_sending_and_is_counted_after():
    w, p = FakeWallet(), Provider(cents=5)
    kp, led = bounded(w, {"p.test": p}, max_total_cents=12)
    prov = dict(P_DICT, payee_pubkey=PAYEE.upper(), capability="summarize")
    for _ in range(2):
        assert (await kp.pay(prov, "/sum", {})).response.status_code == 200
    with pytest.raises(SpendRefused) as e:
        await kp.pay(prov, "/sum", {})
    assert e.value.code == "total" and len(w.sent) == 2
    st = kp.spend_status()
    assert st["total_cents"] == 10 and st["remaining"]["total_cents"] == 2 and st["window"]["calls"] == 2
    rows = led.history()
    assert [r["state"] for r in rows] == ["sent", "sent"] and rows[0]["http"] == 200
    assert rows[0]["txid"] == "02" * 32 and rows[0]["provider"] == PAYEE and rows[0]["capability"] == "summarize"
    assert rows[0]["kas_sompi"] == HOLDER_VALUE_SOMPI + SEND_FEE_SOMPI


@pytest.mark.asyncio
async def test_policy_topup_zero_never_buys():
    w, p = FakeWallet(cents=2), Provider(cents=5)
    kp, led = bounded(w, {"p.test": p}, max_window_topup_cents=0)
    with pytest.raises(SpendRefused) as e:
        await kp.pay(P_DICT, "/sum", {})
    assert e.value.code == "topup" and w.bought == [] and w.sent == []
    assert kp.spend_status()["total_cents"] == 0 and [r["state"] for r in led.history()] == ["void"], "the pay reservation is released"


@pytest.mark.asyncio
async def test_policy_never_swaps_kas_for_a_payment_the_pay_bounds_refuse():
    # the pay is reserved BEFORE the top-up: an agent out of calls for the hour must not keep converting KAS
    w, p = FakeWallet(cents=0), Provider(cents=5)
    kp, _ = bounded(w, {"p.test": p}, max_window_calls=1, max_window_topup_cents=200)
    assert (await kp.pay(P_DICT, "/sum", {})).topped_up["cents"] == 55
    w.cents = 0   # short again, and out of calls for the hour
    with pytest.raises(SpendRefused) as e:
        await kp.pay(P_DICT, "/sum", {})
    assert e.value.code == "window_calls" and w.bought == [55], "no second PSM buy"


@pytest.mark.asyncio
async def test_policy_topup_is_clamped_to_the_remaining_allowance():
    # shortfall 3¢ + 50¢ headroom = 53¢ wanted; 20¢ left on the window: buy 20¢ (still covers the call)
    w, p = FakeWallet(cents=2), Provider(cents=5)
    kp, _ = bounded(w, {"p.test": p}, max_window_topup_cents=20)
    assert (await kp.pay(P_DICT, "/sum", {})).topped_up["cents"] == 20 and w.bought == [20]
    # 2¢ left would not cover the 3¢ shortfall: refused, nothing bought
    w2 = FakeWallet(cents=2)
    kp2, _ = bounded(w2, {"p.test": p}, max_window_topup_cents=2)
    with pytest.raises(SpendRefused) as e:
        await kp2.pay(P_DICT, "/sum", {})
    assert e.value.code == "topup" and w2.bought == [] and w2.sent == []


@pytest.mark.asyncio
async def test_policy_topup_is_reserved_before_buying_and_recorded():
    w, p = FakeWallet(cents=2), Provider(cents=5)
    kp, led = bounded(w, {"p.test": p}, max_window_topup_cents=60)
    res = await kp.pay(P_DICT, "/sum", {})
    assert res.topped_up["cents"] == 53 and w.bought == [53]
    top = [r for r in led.history() if r["kind"] == "topup"]
    assert len(top) == 1 and top[0]["cents"] == 53 and top[0]["state"] == "sent" and top[0]["txid"] == "bb" * 32
    assert top[0]["kas_sompi"] == 53 * 37_000_000 + 7_000_000, "the KAS the PSM actually took, recorded once known"
    assert kp.spend_status()["window"]["topup_cents"] == 53 and kp.spend_status()["total_cents"] == 5
    w.cents = 1   # short again: 54¢ wanted, 7¢ left on the window, which still covers the 4¢ shortfall → buy 7¢
    assert (await kp.pay(P_DICT, "/sum", {})).topped_up["cents"] == 7 and w.bought == [53, 7]
    w.cents = 1   # and now nothing is left: refused before any buy
    with pytest.raises(SpendRefused) as e:
        await kp.pay(P_DICT, "/sum", {})
    assert e.value.code == "topup" and w.bought == [53, 7] and len(w.sent) == 2


@pytest.mark.asyncio
async def test_policy_payment_sent_counts_and_trips_the_failure_limit():
    w, p = FakeWallet(), Provider(cents=5, fail_after_payment=True)
    kp, led = bounded(w, {"p.test": p}, max_window_failures=1)
    with pytest.raises(PaymentSent):
        await kp.pay(P_DICT, "/sum", {})
    st = kp.spend_status()
    assert st["total_cents"] == 5 and st["window"]["failures"] == 1 and led.history()[0]["http"] == 500
    with pytest.raises(SpendRefused) as e:
        await kp.pay(P_DICT, "/sum", {})
    assert e.value.code == "failures" and len(w.sent) == 1, "money moved once and was not served: stop paying"


@pytest.mark.asyncio
async def test_policy_send_error_after_broadcast_stays_counted():
    class Flaky(FakeWallet):
        err = "relay publish failed after broadcast"
        def send(self, to, cents):
            raise KusdError(self.err)
    w, p = Flaky(), Provider(cents=5)
    kp, led = bounded(w, {"p.test": p}, max_total_cents=100)
    with pytest.raises(KusdError, match="relay publish"):
        await kp.pay(P_DICT, "/sum", {})
    assert kp.spend_status()["total_cents"] == 5 and led.history()[0]["state"] == "reserved", "the money may have moved: it counts"
    w.err = "refused before signing: not enough kUSD"
    with pytest.raises(KusdError, match="refused before signing"):
        await kp.pay(P_DICT, "/sum", {})
    assert kp.spend_status()["total_cents"] == 5 and led.history()[0]["state"] == "void", "nothing was signed: it does not count"
    from k402.kusd import KusdNotRun
    class NotRun(FakeWallet):
        def send(self, to, cents):
            raise KusdNotRun("key file must be mode 600")
    kp3, led3 = bounded(NotRun(), {"p.test": p}, max_total_cents=100)
    with pytest.raises(KusdNotRun):
        await kp3.pay(P_DICT, "/sum", {})
    assert kp3.spend_status()["total_cents"] == 0 and led3.history()[0]["state"] == "void", "the binary never ran: it does not count"


@pytest.mark.asyncio
async def test_policy_allow_payees_filters_discovery_and_refuses_direct_pay():
    from k402.channel import payer_pubkey_from_privkey
    w, p = FakeWallet(), Provider(cents=5)
    other = payer_pubkey_from_privkey("cd" * 32)
    reg = [listing(other, "https://other.test/sum", 1, settled_cents=900), listing(PAYEE, "https://p.test/sum", 5)]
    kp, _ = bounded(w, {"p.test": p, "other.test": p}, reg, allow_payees={PAYEE.upper()})
    assert [x["payee_pubkey"] for x in await kp.discover("summarize")] == [PAYEE]
    o = Provider(cents=5)
    kp, _ = bounded(w, {"p.test": p, "other.test": o}, reg, allow_payees={PAYEE.upper()})
    with pytest.raises(SpendRefused) as e:
        await kp.pay({"endpoint": "https://other.test/sum", "payee_pubkey": other}, "/sum", {"text": "private"})
    assert e.value.code == "payee" and w.sent == [] and o.hits == 0, "the request body never reaches a disallowed endpoint"
    route = await kp.pay_best("summarize", json_body={})
    assert route.provider["payee_pubkey"] == PAYEE and w.sent == [(PAY_TO, 5)]


@pytest.mark.asyncio
async def test_policy_requires_a_listed_payee():
    w, p = FakeWallet(), Provider(cents=5)
    kp, _ = bounded(w, {"p.test": p})
    with pytest.raises(SpendRefused) as e:
        await kp.pay({"endpoint": "https://p.test/sum"}, "/sum", {})
    assert e.value.code == "unlisted_payee" and w.sent == []
    kp, _ = bounded(w, {"p.test": p}, require_listed_payee=False)
    assert (await kp.pay({"endpoint": "https://p.test/sum"}, "/sum", {})).response.status_code == 200


@pytest.mark.asyncio
async def test_policy_allow_capabilities():
    w, p = FakeWallet(), Provider(cents=5)
    reg = [listing(PAYEE, "https://p.test/sum", 5)]
    kp, _ = bounded(w, {"p.test": p}, reg, allow_capabilities={"summarize"})
    with pytest.raises(SpendRefused) as e:
        await kp.pay(dict(P_DICT, capability="extract"), "/sum", {})
    assert e.value.code == "capability"
    with pytest.raises(SpendRefused) as e:
        await kp.pay(P_DICT, "/sum", {})            # a provider dict naming no capability cannot be allowed
    assert e.value.code == "capability"
    with pytest.raises(SpendRefused) as e:
        await kp.pay_best("extract", json_body={})
    assert e.value.code == "capability" and w.sent == []
    assert (await kp.pay_best("summarize", json_body={})).response.status_code == 200


@pytest.mark.asyncio
async def test_pay_best_does_not_fail_over_on_a_spend_refusal():
    w = FakeWallet()
    a, b = Provider(cents=5), Provider(cents=5)
    reg = [listing(PAYEE, "https://a.test/sum", 5), listing("cd" * 32, "https://b.test/sum", 5)]
    kp, _ = bounded(w, {"a.test": a, "b.test": b}, reg, max_total_cents=3)
    with pytest.raises(SpendRefused) as e:
        await kp.pay_best("summarize", json_body={})
    assert e.value.code == "total" and a.paid == [] and b.paid == [] and w.sent == []


@pytest.mark.asyncio
async def test_frozen_ledger_refuses_before_the_first_request_leaves():
    w, p = FakeWallet(), Provider(cents=5)
    kp, led = bounded(w, {"p.test": p}, max_total_cents=100)
    led.freeze()
    with pytest.raises(SpendRefused) as e:
        await kp.pay(P_DICT, "/sum", {"text": "private"})
    assert e.value.code == "frozen" and p.hits == 0 and w.sent == []


@pytest.mark.asyncio
async def test_payment_sent_when_the_paid_request_itself_fails():
    # the box is built, then the provider never answers the paid request (transport error): that is a PaymentSent
    # (status 0), it counts as a failure, and pay_best does not pay anyone else
    w, p = FakeWallet(), Provider(cents=5, timeout_after_payment=True)
    kp, led = bounded(w, {"p.test": p}, max_window_failures=1)
    with pytest.raises(PaymentSent) as e:
        await kp.pay(P_DICT, "/sum", {})
    assert e.value.txid == "01" * 32 and e.value.status == 0 and e.value.response is None and "no HTTP answer" in str(e.value)
    assert kp.spend_status()["window"]["failures"] == 1 and led.history()[0]["http"] == 0
    with pytest.raises(SpendRefused) as e2:
        await kp.pay(P_DICT, "/sum", {})
    assert e2.value.code == "failures" and len(w.sent) == 1
    # pay_best: provider A takes the box and goes quiet; provider B is never paid
    w, a, b = FakeWallet(), Provider(cents=5, timeout_after_payment=True), Provider(cents=5)
    reg = [listing(PAYEE, "https://a.test/sum", 5, settled_cents=9), listing("cd" * 32, "https://b.test/sum", 5)]
    with pytest.raises(PaymentSent):
        await payer(w, {"a.test": a, "b.test": b}, reg).pay_best("summarize", json_body={})
    assert b.paid == [] and len(w.sent) == 1
    # the wallet failing to give its key for the proof after the box exists is the same situation
    class NoKey(FakeWallet):
        def payee_xonly(self):
            raise KusdError("kusd_pay balance answered without the wallet key")
    w = NoKey()
    with pytest.raises(PaymentSent) as e3:
        await payer(w, {"p.test": Provider(cents=5)}).pay(P_DICT, "/sum", {})
    assert e3.value.status == 0 and len(w.sent) == 1


# ------------------------------------------------------------------ Phase 3: a quote without a payment
@pytest.mark.asyncio
async def test_payer_quote_checks_everything_and_pays_nothing():
    w, p = FakeWallet(), Provider(cents=5)
    kp = payer(w, {"p.test": p})
    q = await kp.quote(dict(P_DICT, capability="summarize"), "/sum", {"text": "hi"})
    assert (q.cents, q.pay_to, q.payment_id, q.wire, q.authority) == (5, PAY_TO, PID, "x402", AUTH)
    assert q.outlay.price_cents == 5 and q.outlay.kas_sompi == HOLDER_VALUE_SOMPI + SEND_FEE_SOMPI
    assert w.sent == [] and p.paid == [] and p.hits == 1, "one unpaid request, no box, no proof"
    # the same refusals as pay(), before anything is built
    with pytest.raises(KusdError, match="payee mismatch"):
        await payer(w, {"p.test": Provider(cents=5, pay_to=derive_payee(payer_pubkey_from_privkey("cd" * 32), PID))}).quote(P_DICT, "/sum", {})
    kpp, _ = bounded(w, {"p.test": Provider(cents=50)}, max_per_call_cents=10)
    with pytest.raises(SpendRefused) as e:
        await kpp.quote(P_DICT, "/sum", {})
    assert e.value.code == "per_call" and w.sent == []
    # a free endpoint quotes nothing to pay
    free = await payer(w, {"p.test": lambda r: httpx.Response(200, json={"ok": True})}).quote({"endpoint": "https://p.test/x"}, "/x", {})
    assert free is None
