# The agent side of the kUSD market: KusdPayer, a sibling of ChannelPayer (discover / rank / preflight / pay /
# pay_best) that settles each call with a kUSD holder box. x402 first: read the 402's PAYMENT-REQUIRED header,
# pick the `exact` requirement whose asset is our kUSD family, `wallet.send`, retry with PAYMENT-SIGNATURE.
# k402's own wire is the fallback for a provider that answers with only the k402 body (kusd-utxo offer +
# X-K402-Payment). Before any box is built the quote is checked: our family, our network, a price within the
# cap (the listing's price by default), and a payee that is the listed provider's key tweaked by the offer
# (so a hijacked endpoint cannot redirect the money). After the box is built there is no fail-over: a
# "not indexed yet" 402 is retried with the same txid, anything else raises PaymentSent with the txid.
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

import httpx

from . import x402
from .channel_client import RouteError, RouteResult
from .kusd import KusdError
from .kusd_keys import derive_payee
from .registry import RegistryClient
from .schemes import PAYMENT_HEADER, SCHEME_KUSD, KusdOffer, parse_offers
from .spend import SpendLedger, SpendPolicy, SpendRefused

# kusd_pay says this when it stopped before signing anything: the reservation is then void. Any other failure of a
# send/buy keeps counting against the budget, because the transaction may have been broadcast.
REFUSED_BEFORE_SIGNING = "refused before signing"

# every kUSD box carries this much KAS (kusd-client HOLDER_VALUE), which the recipient owns too and recovers
# when it merges or spends the box; the network fee is an upper bound (a `send` costs ~0.07 KAS on TN10)
HOLDER_VALUE_SOMPI = 150_000_000
SEND_FEE_SOMPI = 10_000_000

# x402 PAYMENT-RESPONSE errorReasons after which the same payment is presented again
RETRYABLE_X402 = {"payment_not_found", "verifier_unavailable"}


@dataclass(frozen=True)
class Outlay:
    """What one kUSD payment costs the payer in full: the price, the KAS locked in the box, the network fee."""
    price_cents: int
    box_kas_sompi: int = HOLDER_VALUE_SOMPI
    fee_sompi: int = SEND_FEE_SOMPI

    @property
    def kas_sompi(self) -> int:
        return self.box_kas_sompi + self.fee_sompi


@dataclass
class Quote:
    """A provider's kUSD demand, read off its 402 on whichever wire it spoke."""
    cents: int
    pay_to: str
    authority: str
    payment_id: str
    network: str                    # as the offer named it (k402 name or x402 id)
    wire: str                       # "x402" | "k402"
    accepted: Optional[dict] = None # the x402 requirement we accept (x402 wire only)


@dataclass
class PayResult:
    response: httpx.Response
    txid: str
    cents: int
    wire: str
    outlay: Outlay
    topped_up: Optional[dict] = None  # the PSM buy made first, when the wallet was short
    attempts: int = 1                 # paid requests sent (retries while the payment was unindexed)


class PaymentSent(KusdError):
    """The box was built and broadcast, but the provider did not serve the call. Carries the txid: the money
    moved, so the caller must not pay anyone else for this call without deciding what to do about it."""

    def __init__(self, txid: str, cents: int, provider: dict, response: httpx.Response, attempts: int):
        self.txid, self.cents, self.provider, self.response, self.attempts = txid, cents, provider, response, attempts
        self.status = response.status_code
        super().__init__(f"paid {cents}¢ (tx {txid[:8]}…) to {provider.get('payee_pubkey', provider.get('endpoint'))} but got HTTP {self.status} after {attempts} attempt(s)")


def _base_url(endpoint: str) -> str:
    u = urlsplit(endpoint)
    return f"{u.scheme}://{u.netloc}"


class KusdPayer:
    def __init__(self, wallet, registry_url: Optional[str] = None, network: str = "testnet-10",
                 http: Optional[httpx.AsyncClient] = None, headroom_cents: int = 50,
                 retry_delays: tuple = (2, 4, 8, 15), *, policy: Optional[SpendPolicy] = None,
                 ledger: Optional[SpendLedger] = None):
        """`policy` is the operator's SpendPolicy; it needs a `ledger` (a SpendLedger file) so the budget
        survives a restart. Without a policy the payer is bounded only by the caps each call passes."""
        if policy is not None and ledger is None:
            raise ValueError("a SpendPolicy needs a ledger (SpendLedger): an unpersisted budget resets on every restart")
        self.wallet = wallet
        self.network = network
        self.http = http or httpx.AsyncClient(timeout=180)
        self.headroom_cents = headroom_cents
        self.retry_delays = retry_delays
        self.policy, self.ledger = policy, ledger
        self._registry = RegistryClient(registry_url, http=self.http) if registry_url else None
        self._family = None

    # -------- the operator's bounds --------
    def spend_status(self) -> Optional[dict]:
        """What the operator's policy has left (SpendLedger.status), or None when there is no policy."""
        return None if self.policy is None else self.ledger.status(self.policy)

    def _gate(self, provider: dict, capability: Optional[str]) -> None:
        """Refuse before anything is reserved: a capability or payee the operator did not allow, or a bare
        endpoint whose quote cannot be checked against a listed key."""
        pol = self.policy
        if pol is None:
            return
        if pol.allow_capabilities is not None and capability not in pol.allow_capabilities:
            raise SpendRefused("capability", f"{capability or 'a provider naming no capability'} is not in the operator's allowed capabilities",
                               self.ledger.status(pol))
        listed = (provider.get("payee_pubkey") or "").lower()
        if not listed:
            if pol.require_listed_payee:
                raise SpendRefused("unlisted_payee", "the provider has no listed payee key to check the quote against", self.ledger.status(pol))
        elif pol.allow_payees is not None and listed not in pol.allow_payees:
            raise SpendRefused("payee", f"{listed[:8]}… is not in the operator's allowed payees", self.ledger.status(pol))

    def _reserve(self, kind: str, cents: int, provider: dict, kas_sompi: int) -> Optional[int]:
        if self.policy is None:
            return None
        return self.ledger.reserve(self.policy, kind=kind, cents=cents, provider=(provider.get("payee_pubkey") or provider.get("endpoint") or ""),
                                   capability=provider.get("capability") or "", kas_sompi=kas_sompi)

    def _failed(self, rid: Optional[int], e: BaseException) -> None:
        """A send/buy raised: void the reservation only when kusd_pay says it never signed."""
        if rid is not None and isinstance(e, KusdError) and REFUSED_BEFORE_SIGNING in str(e).lower():
            self.ledger.void(rid)

    def _settle(self, rid: Optional[int], **kw) -> None:
        if rid is not None:
            self.ledger.settle(rid, **kw)

    async def family(self):
        if self._family is None:
            self._family = await asyncio.to_thread(self.wallet.family)
        return self._family

    # -------- discovery + client-side routing --------
    async def discover(self, capability: str = "", max_price_cents: Optional[int] = None, limit: int = 20) -> list:
        """kUSD-priced providers from the registry (ranked by settled cents, then price)."""
        if not self._registry:
            raise RuntimeError("no registry_url configured")
        found = await self._registry.search(capability=capability, currency="kUSD", limit=limit)
        if max_price_cents is not None:
            found = [p for p in found if int(p.get("price_cents", 0)) <= max_price_cents]
        if self.policy is not None and self.policy.allow_payees is not None:
            found = [p for p in found if (p.get("payee_pubkey") or "").lower() in self.policy.allow_payees]
        return found

    @staticmethod
    def rank(providers: list, policy: str = "registry") -> list:
        """registry (the registry's order) | cheapest (price_cents ascending) | reputation (settled cents desc)."""
        if policy == "cheapest":
            return sorted(providers, key=lambda p: int(p.get("price_cents", 1 << 62)))
        if policy == "reputation":
            return sorted(providers, key=lambda p: -int((p.get("reputation") or {}).get("settled_cents", 0)))
        return list(providers)

    # -------- the 402, on either wire --------
    def _quote(self, r: httpx.Response) -> Quote:
        """Read a 402: the x402 header's kUSD requirement when present, else the k402 body's kusd-utxo offer."""
        if r.status_code != 402:
            raise KusdError(f"expected a 402 pay-gate, got HTTP {r.status_code}")
        hdr = r.headers.get(x402.HDR_REQUIRED)
        if hdr:
            pr = x402.decode_required_header(hdr)
            for req in pr.get("accepts") or []:
                if req.get("scheme") == x402.SCHEME_EXACT and x402.is_kusd(req):
                    extra = req.get("extra") or {}
                    return Quote(cents=int(req["amount"]), pay_to=req["payTo"], authority=req["asset"], network=req.get("network", ""),
                                 payment_id=extra.get("payment_id", ""), wire="x402", accepted=req)
        try:
            body = r.json()
            offers = parse_offers(body) if isinstance(body, dict) and "k402" in body else []
        except (ValueError, KeyError, TypeError):
            offers = []
        for o in offers:
            if isinstance(o, KusdOffer):
                return Quote(cents=int(o.amount_cents), pay_to=o.pay_to_pubkey, authority=o.authority, network=o.network,
                             payment_id=o.payment_id, wire="k402")
        raise KusdError("the provider's 402 carries no kUSD offer on either wire")

    async def _check(self, q: Quote, max_price_cents: Optional[int], provider: dict) -> None:
        """Refuse before any box is built: another family's kUSD, another network, a price above the cap, a
        payee that is not the listed provider's key for this offer, a malformed payment_id."""
        family = await self.family()
        if q.authority.lower() != family.authority.lower():
            raise KusdError(f"asset mismatch: the provider wants kUSD of family {q.authority[:8]}…, this wallet holds {family.authority[:8]}…")
        if q.network not in (self.network, x402.network_id(self.network)):
            raise KusdError(f"network mismatch: the offer is on {q.network}, this payer is on {self.network}")
        if max_price_cents is not None and q.cents > max_price_cents:
            raise KusdError(f"price {q.cents}¢ is above the cap of {max_price_cents}¢")
        if q.cents <= 0:
            raise KusdError("the provider quoted a non-positive price")
        if not q.payment_id:
            raise KusdError("the offer names no payment_id")
        listed = provider.get("payee_pubkey")
        if listed:
            try:
                expected = derive_payee(listed, q.payment_id)
            except (ValueError, TypeError):
                raise KusdError("the listed payee key is not a valid x-only public key") from None
            if q.pay_to.lower() != expected:
                raise KusdError("payee mismatch: the quote is not paid to the listed provider's key for this offer")

    @staticmethod
    def outlay(cents: int) -> Outlay:
        return Outlay(price_cents=int(cents))

    async def ensure_funds(self, cents: int, provider: Optional[dict] = None) -> Optional[dict]:
        """Top the wallet up through the PSM when its kUSD is short: the shortfall plus `headroom_cents`
        (the agent pays KAS for it, and the PSM's fee). Under a policy the buy is reserved first (kind
        "topup") and refused when it would cross `max_window_topup_cents`. Returns the buy, or None."""
        have = int((await asyncio.to_thread(self.wallet.balance)).get("kusd_cents", 0))
        if have >= cents:
            return None
        need = cents - have + self.headroom_cents
        rid = self._reserve("topup", need, provider or {}, 0)
        try:
            bought = await asyncio.to_thread(self.wallet.buy, need)
        except BaseException as e:
            self._failed(rid, e)
            raise
        self._settle(rid, txid=str(bought.get("txid", "")).lower(), kas_sompi=int(bought.get("kas_in", 0)) + int(bought.get("fee", 0)))
        return bought

    def _paid_headers(self, q: Quote, txid: str, payer: str) -> dict:
        if q.wire == "x402":
            pp = x402.payment_payload(accepted=q.accepted, txid=txid, payer=payer)
            return {x402.HDR_SIGNATURE: x402.encode_signature_header(pp)}
        return {PAYMENT_HEADER: f"{SCHEME_KUSD} {txid} {q.payment_id}"}

    @staticmethod
    def _retryable(r: httpx.Response) -> bool:
        """A 402 after payment that only means 'not indexed yet': the k402 body says so, or the x402
        PAYMENT-RESPONSE's errorReason is payment_not_found / verifier_unavailable."""
        if r.status_code != 402:
            return False
        resp = r.headers.get(x402.HDR_RESPONSE)
        if resp:
            try:
                if x402.decode_response_header(resp).get("errorReason") in RETRYABLE_X402:
                    return True
            except (ValueError, TypeError, KeyError):
                pass
        try:
            reason = (r.json().get("reason") or "").lower()
        except (ValueError, AttributeError):
            return False
        return "not indexed" in reason or "retry" in reason

    # -------- pay one call --------
    async def pay(self, provider: dict, path: str = "", json_body: Optional[dict] = None, *,
                  max_price_cents: Optional[int] = None, ensure: bool = True) -> PayResult:
        """Pay one call to `provider` (a discover() result or {"endpoint": url, "payee_pubkey"?}) in kUSD:
        fetch the 402, check the quote, top up if short, build the box, retry with the proof; wait out "not
        indexed yet" on the same txid. Raises KusdError before any box is built when the quote is refused,
        PaymentSent when the box was built but the call was not served."""
        base = _base_url(provider["endpoint"])
        p_path = path or (urlsplit(provider["endpoint"]).path or "/")
        url = base + (p_path if p_path.startswith("/") else "/" + p_path)
        first = await self.http.post(url, json=json_body or {})
        if first.status_code < 400:
            return PayResult(response=first, txid="", cents=0, wire="free", outlay=Outlay(0, 0, 0), attempts=0)
        q = self._quote(first)
        self._gate(provider, provider.get("capability"))   # the operator's answer does not depend on the quote
        await self._check(q, self.policy.cap(max_price_cents) if self.policy else max_price_cents, provider)
        topped = await self.ensure_funds(q.cents, provider) if ensure else None
        rid = self._reserve("pay", q.cents, provider, self.outlay(q.cents).kas_sompi)
        try:
            sent = await asyncio.to_thread(self.wallet.send, q.pay_to, q.cents)
        except BaseException as e:
            self._failed(rid, e)
            raise
        txid = str(sent["txid"]).lower()
        self._settle(rid, txid=txid)
        headers = self._paid_headers(q, txid, await asyncio.to_thread(self.wallet.payee_xonly))
        attempts = 0
        delays = iter(self.retry_delays)
        while True:
            attempts += 1
            r = await self.http.post(url, json=json_body or {}, headers=headers)
            if not self._retryable(r):
                break
            d = next(delays, None)
            if d is None:
                break
            await asyncio.sleep(d)
        self._settle(rid, http=r.status_code)
        if r.status_code >= 400:
            raise PaymentSent(txid, q.cents, provider, r, attempts)
        return PayResult(response=r, txid=txid, cents=q.cents, wire=q.wire, outlay=self.outlay(q.cents),
                         topped_up=topped, attempts=attempts)

    # -------- preflight: is this a live kUSD pay-gate? --------
    async def preflight(self, provider: dict) -> bool:
        try:
            r = await self.http.post(provider["endpoint"], json={})
            self._quote(r)
        except (httpx.HTTPError, KusdError):
            return False
        return True

    # -------- auto-route --------
    async def pay_best(self, capability: str, path: str = "", json_body: Optional[dict] = None, *,
                       policy: str = "registry", max_tries: int = 3, max_price_cents: Optional[int] = None,
                       preflight: bool = True) -> RouteResult:
        """Discover kUSD providers for `capability`, rank them, pay the first that serves the call, fail over
        on a refused quote or a dead endpoint up to `max_tries` — never after a box was built (PaymentSent
        propagates). The cap is `max_price_cents`, else each provider's listed price. The RouteResult's
        `result` is the PayResult. A SpendRefused (the operator's bounds) propagates at once: the next provider
        would meet the same budget."""
        if self.policy is not None and self.policy.allow_capabilities is not None and capability not in self.policy.allow_capabilities:
            raise SpendRefused("capability", f"{capability} is not in the operator's allowed capabilities", self.ledger.status(self.policy))
        providers = self.rank(await self.discover(capability, max_price_cents=max_price_cents), policy)
        attempts: list = []
        tries = 0
        for provider in providers:
            payee = provider.get("payee_pubkey", "?")
            if preflight and not await self.preflight(provider):
                attempts.append((payee, "preflight: not a live kUSD pay-gate"))
                continue
            tries += 1
            cap = max_price_cents if max_price_cents is not None else (int(provider["price_cents"]) if provider.get("price_cents") else None)
            try:
                res = await self.pay(provider, path, json_body, max_price_cents=cap)
            except (PaymentSent, SpendRefused):
                raise
            except (KusdError, httpx.HTTPError) as e:
                attempts.append((payee, f"error: {e}"))
            else:
                out = RouteResult(response=res.response, provider=provider, attempts=attempts)
                out.result = res
                return out
            if tries >= max_tries:
                break
        raise RouteError(capability, attempts)

    async def aclose(self):
        await self.http.aclose()
