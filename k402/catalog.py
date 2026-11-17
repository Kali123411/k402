"""Attested listings: the registry probes an x402 endpoint itself and lists what the endpoint actually offers.

A signed listing (`k402.registry.Listing`) is a provider's own signed act. Many pay-per-call services have no
k402 key — a plain x402 endpoint, a Base service behind the router — and for those the registry is the witness:
one unpaid GET, an HTTP 402 with a valid offer (the x402 `PAYMENT-REQUIRED` header or a k402 body challenge),
and the terms *the endpoint served* become the listing. Declared values may only confirm them. The same probe
runs again later (`reprobe`), so an endpoint that stops answering stops being offered to agents.

Nothing here pays; nothing here trusts the submitter."""
from __future__ import annotations

import ipaddress
import json
import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from .x402 import decode_required_header, network_id

MIN_AMOUNT_SOMPI = 50_000_000          # the catalog's price floor for KAS listings: below is dust (KIP-9 mass)
PROBE_TIMEOUT = 9.0
DEAD_AFTER = 3                         # consecutive failed probes before a listing is retired
SCHEMES = ("exact", "batch-settlement", "kaspa-utxo", "kaspa-channel", "kusd-utxo")


@dataclass
class Requirement:
    scheme: str
    network: str                       # x402 network id: kaspa:mainnet | kaspa:testnet-10
    amount_sompi: Optional[int]        # KAS price (None for kUSD)
    pay_to: str
    currency: str = "KAS"              # KAS | kUSD
    binding: Optional[str] = None
    profile: Optional[str] = None
    cents: Optional[int] = None        # kUSD price
    asset: str = ""


@dataclass
class Attestation:
    endpoint: str
    ok: bool
    status: int = 0
    wire: str = ""                     # x402 | k402
    latency_ms: int = 0
    probed_at: int = 0
    error: str = ""
    requirements: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {**asdict(self), "requirements": [asdict(r) for r in self.requirements]}


def _is_private(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified


def _default_resolver(host: str) -> list:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})


def assert_public_url(raw: str, *, resolver: Callable[[str], list] = _default_resolver) -> str:
    """The URL the registry may probe: http(s), a host that is not ours (no private, loopback or link-local
    address — checked after resolution, so a public name pointing inside is refused too)."""
    try:
        u = urlsplit(str(raw).strip())
    except ValueError as e:
        raise ValueError(f"not a URL: {e}")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("endpoint must be an http(s) URL")
    host = u.hostname
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("endpoint must be reachable from the public internet")
    try:
        ips = [host] if _looks_like_ip(host) else resolver(host)
    except (socket.gaierror, OSError) as e:
        raise ValueError(f"endpoint host does not resolve: {e}")
    if not ips:
        raise ValueError("endpoint host does not resolve")
    for ip in ips:
        try:
            if _is_private(ip):
                raise ValueError(f"endpoint resolves to a non-public address ({ip})")
        except ValueError:
            raise
    return u.geturl()


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _network(n: str) -> str:
    """Either spelling in, the x402 id out."""
    if not n:
        return ""
    if n.startswith("kaspa:"):
        return n
    try:
        return network_id(n)
    except Exception:
        return f"kaspa:{n}"


def _requirements_from_x402(accepts: list, kusd_authority: str) -> list:
    out = []
    for a in accepts or []:
        if not isinstance(a, dict):
            continue
        extra = a.get("extra") if isinstance(a.get("extra"), dict) else {}
        asset = str(a.get("asset", ""))
        kusd = bool(kusd_authority) and asset.lower() == kusd_authority.lower() or extra.get("binding", "").startswith("kusd-")
        amount = a.get("amount", a.get("maxAmountRequired"))
        try:
            amount_i = int(str(amount))
        except (TypeError, ValueError):
            amount_i = None
        out.append(Requirement(scheme=str(a.get("scheme", "")), network=_network(str(a.get("network", ""))),
                               amount_sompi=None if kusd else amount_i, pay_to=str(a.get("payTo", "")),
                               currency="kUSD" if kusd else "KAS", binding=extra.get("binding"), profile=extra.get("profile"),
                               cents=amount_i if kusd else None, asset=asset))
    return out


def _requirements_from_k402(body: dict, kusd_authority: str) -> list:
    out = []
    for a in body.get("accepts") or []:
        if not isinstance(a, dict):
            continue
        scheme = str(a.get("scheme", ""))
        kusd = scheme == "kusd-utxo"
        amount = a.get("amount_sompi") if not kusd else a.get("cents", a.get("amount_cents"))
        try:
            amount_i = int(str(amount))
        except (TypeError, ValueError):
            amount_i = None
        out.append(Requirement(scheme=scheme, network=_network(str(a.get("network", ""))), amount_sompi=None if kusd else amount_i,
                               pay_to=str(a.get("pay_to", a.get("payee_pubkey", ""))), currency="kUSD" if kusd else "KAS",
                               cents=amount_i if kusd else None, asset=str(a.get("authority", "")) if kusd else "KAS"))
    return out


async def probe(endpoint: str, http: httpx.AsyncClient, *, kusd_authority: str = "", timeout: float = PROBE_TIMEOUT) -> Attestation:
    """One unpaid GET. Never raises for the endpoint's sake: the Attestation says what happened."""
    t0 = time.monotonic()
    att = Attestation(endpoint=endpoint, ok=False, probed_at=int(time.time()))
    try:
        r = await http.get(endpoint, headers={"accept": "application/json"}, follow_redirects=False, timeout=timeout)
    except httpx.TimeoutException:
        att.error = f"no response within {timeout:g}s (timeout)"
        return att
    except httpx.HTTPError as e:
        att.error = f"could not connect: {e.__class__.__name__}: {e}"
        return att
    att.latency_ms = int((time.monotonic() - t0) * 1000)
    att.status = r.status_code
    if 300 <= r.status_code < 400:
        att.error = f"endpoint redirected (HTTP {r.status_code}); list the final URL"
        return att
    if r.status_code != 402:
        att.error = f"expected HTTP 402 to an unpaid request, got {r.status_code}"
        return att
    hdr = r.headers.get("payment-required")
    body = None
    try:
        body = r.json()
    except ValueError:
        pass
    if hdr:
        try:
            pr = decode_required_header(hdr)
            accepts = pr.get("accepts")
            if not isinstance(accepts, list):
                raise ValueError("no accepts")
        except Exception as e:
            att.error = f"malformed PAYMENT-REQUIRED header: {e}"
            return att
        att.wire = "x402"
        att.requirements = _requirements_from_x402(accepts, kusd_authority)
    elif isinstance(body, dict) and "accepts" in body and ("k402" in body or body.get("accepts")):
        att.wire = "k402"
        att.requirements = _requirements_from_k402(body, kusd_authority)
    else:
        att.error = "402 without an offer: no PAYMENT-REQUIRED header and no k402 challenge body"
        return att
    good = [q for q in att.requirements if q.scheme and q.network and q.pay_to]
    if not good:
        att.error = "the offer carries no usable requirement (scheme, network and payTo are needed)"
        return att
    att.requirements = good
    att.ok = True
    return att


def reconcile(declared: dict, att: Attestation) -> dict:
    """The live offer is the truth; what the submitter declared may only agree with it. Returns the
    authoritative terms of one requirement (the one matching the declared scheme/network, else the first)."""
    if not att.ok or not att.requirements:
        raise ValueError(att.error or "no live offer")
    scheme = declared.get("scheme") or ""
    network = _network(declared.get("network") or "")
    cands = [q for q in att.requirements if (not scheme or q.scheme == scheme) and (not network or q.network == network)]
    if not cands:
        offered = ", ".join(f"{q.scheme}/{q.network}" for q in att.requirements)
        raise ValueError(f"endpoint does not offer {scheme or '*'}/{network or '*'}; it offers: {offered}")
    q = cands[0]
    if declared.get("pay_to") and declared["pay_to"] != q.pay_to:
        raise ValueError(f"declared payTo does not match the endpoint's payTo ({q.pay_to})")
    if declared.get("amount_sompi") not in (None, "") and q.amount_sompi is not None and int(declared["amount_sompi"]) != q.amount_sompi:
        raise ValueError(f"declared amount ({declared['amount_sompi']}) does not match the endpoint's amount ({q.amount_sompi} sompi)")
    return {"scheme": q.scheme, "network": q.network, "pay_to": q.pay_to, "amount_sompi": q.amount_sompi, "currency": q.currency,
            "cents": q.cents, "binding": q.binding, "profile": q.profile, "asset": q.asset}


def meets_floor(terms: dict) -> bool:
    if terms.get("currency") == "kUSD":
        return (terms.get("cents") or 0) >= 1
    return (terms.get("amount_sompi") or 0) >= MIN_AMOUNT_SOMPI


def dumps(v) -> str:
    return json.dumps(v, separators=(",", ":"), sort_keys=True)
