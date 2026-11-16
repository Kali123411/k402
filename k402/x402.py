# k402/x402.py — x402 V2 compatibility layer for k402 (Kaspa network binding).
#
# A WIRE ADAPTER: it translates between k402's native offer/verify flow and the x402 V2 wire
# protocol so x402-aware clients, servers, and facilitators can settle on Kaspa. It does NOT change
# how payments are checked on-chain — the server feeds the extracted fields to k402's existing engine.
#
# Types match x402 V2 exactly (authoritative: typescript/packages/core/src/schemas/index.ts,
# the *V2Schema exports; spec specs/x402-specification-v2.md + specs/transports-v2/http.md):
#   PAYMENT-REQUIRED  (server->client, HTTP 402): base64(PaymentRequired{x402Version:2, resource, accepts:[...]})
#   PAYMENT-SIGNATURE (client->server, retry):    base64(PaymentPayload{x402Version:2, accepted, payload})
#   PAYMENT-RESPONSE  (server->client, settle):   base64(SettleResponse{success, transaction, network, ...})
#
# Kaspa "exact" binding: scheme "exact", network CAIP-2 "kaspa:mainnet"/"kaspa:testnet-10", asset "KAS",
# amounts are sompi (atomic; 1 KAS = 100_000_000 sompi). assetTransferMethod "submitted": the client
# broadcasts a Kaspa tx paying `amount` to the per-offer `payTo`, then proves it with the txid; the
# facilitator confirms the address received it (k402's address_received engine). extra fields are all
# consumed (payment_id correlates the offer; assetTransferMethod selects the payload shape) per the
# new-network authoring rules.

import base64
import json
from typing import Any, Optional

X402_VERSION = 2                       # x402Version is literally 2 in V2

# ---- V2 headers ----
HDR_REQUIRED = "PAYMENT-REQUIRED"      # server -> client, with 402
HDR_SIGNATURE = "PAYMENT-SIGNATURE"    # client -> server, on retry
HDR_RESPONSE = "PAYMENT-RESPONSE"      # server -> client, after settle

# ---- Kaspa network binding ----
NETWORK_MAINNET = "kaspa:mainnet"      # CAIP-2 (namespace:reference)
NETWORK_TESTNET = "kaspa:testnet-10"
ASSET_KAS = "KAS"                      # native KAS; amounts in sompi. (CAIP-19 form: kaspa:mainnet/slip44:111)
SCHEME_EXACT = "exact"
XFER_SUBMITTED = "submitted"           # extra.assetTransferMethod for the push/confirm-receipt flow


# ---------- base64-JSON codecs ----------
def b64e_json(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()

def b64d_json(s: str) -> dict:
    return json.loads(base64.b64decode(s.encode()).decode())

def _prune(d: dict) -> dict:
    """Drop None values — x402 impls serialize with exclude_none."""
    return {k: v for k, v in d.items() if v is not None}


# ---------- server side: build the 402 ----------
def resource_info(url: str, *, description: Optional[str] = None, mime_type: Optional[str] = None,
                  service_name: Optional[str] = None, tags: Optional[list] = None,
                  icon_url: Optional[str] = None) -> dict:
    """ResourceInfo (top-level on PaymentRequired in V2)."""
    return _prune({"url": url, "description": description, "mimeType": mime_type,
                   "serviceName": service_name, "tags": tags, "iconUrl": icon_url})

def requirements(*, network: str, amount_sompi: int, pay_to: str, payment_id: str,
                 max_timeout_seconds: int = 120, asset: str = ASSET_KAS,
                 extra: Optional[dict] = None) -> dict:
    """A V2 PaymentRequirements entry for the Kaspa exact binding."""
    ex = {"payment_id": payment_id, "assetTransferMethod": XFER_SUBMITTED}
    if extra:
        ex.update(extra)
    return {"scheme": SCHEME_EXACT, "network": network, "amount": str(amount_sompi),
            "asset": asset, "payTo": pay_to, "maxTimeoutSeconds": max_timeout_seconds, "extra": ex}

_NETWORK_IDS = {"testnet-10": NETWORK_TESTNET, "mainnet": NETWORK_MAINNET}

def network_id(network: str) -> str:
    """The x402 network id for a k402 network name ("testnet-10" -> "kaspa:testnet-10"); an x402 id passes
    through. Anything else is refused rather than guessed (a testnet-11 offer must never say testnet-10)."""
    if network in (NETWORK_TESTNET, NETWORK_MAINNET):
        return network
    try:
        return _NETWORK_IDS[network]
    except KeyError:
        raise ValueError(f"unknown network '{network}': x402 bindings exist for testnet-10 and mainnet") from None


# ---- kUSD as an asset of the Kaspa network (DESIGN-kusd-market.md §1) ----
KUSD_SYMBOL = "kUSD"
KUSD_BINDING = "kusd-holder-v1"

def kusd_requirements(*, network: str, amount_cents: int, pay_to_pubkey: str, authority: str, payment_id: str,
                      max_timeout_seconds: int = 120) -> dict:
    """x402 V2 PaymentRequirements for a kUSD payment: `asset` is the kUSD authority's covenant id (the
    token's on-chain identity), `amount` is cents, `payTo` the payee's x-only key (a kUSD box owner), and
    the payer proves with the creating txid (assetTransferMethod "submitted")."""
    return {"scheme": SCHEME_EXACT, "network": network, "amount": str(int(amount_cents)), "asset": authority,
            "payTo": pay_to_pubkey, "maxTimeoutSeconds": max_timeout_seconds,
            "extra": {"assetSymbol": KUSD_SYMBOL, "decimals": 2, "assetTransferMethod": XFER_SUBMITTED,
                      "payment_id": payment_id, "binding": KUSD_BINDING}}

def is_kusd(accepted: dict) -> bool:
    """Whether an accepted requirement is the kUSD binding (by its binding tag, never by guessing the asset)."""
    extra = (accepted or {}).get("extra") or {}
    return extra.get("binding") == KUSD_BINDING

def payment_required(*, resource: dict, accepts: list, error: Optional[str] = None,
                     extensions: Optional[dict] = None) -> dict:
    """The PaymentRequired object (base64'd into the PAYMENT-REQUIRED header)."""
    if not accepts:
        raise ValueError("accepts must have at least one PaymentRequirements entry")
    return _prune({"x402Version": X402_VERSION, "error": error, "resource": resource,
                   "accepts": accepts, "extensions": extensions})

def encode_required_header(pr: dict) -> str:
    return b64e_json(pr)

def decode_required_header(h: str) -> dict:
    return b64d_json(h)


# ---------- client side: build the payment (also used by tests) ----------
def payment_payload(*, accepted: dict, txid: str, payer: Optional[str] = None,
                    resource: Optional[dict] = None, extensions: Optional[dict] = None) -> dict:
    """A V2 PaymentPayload. `accepted` is the single requirement the client chose from `accepts`
    (scheme/network/payTo/amount/extra live inside it — V2 has no top-level scheme/network)."""
    return _prune({"x402Version": X402_VERSION, "resource": resource, "accepted": accepted,
                   "payload": _prune({"transaction": txid, "payer": payer}),
                   "extensions": extensions})

def encode_signature_header(pp: dict) -> str:
    return b64e_json(pp)

def decode_signature_header(h: str) -> dict:
    return b64d_json(h)

def decode_response_header(h: str) -> dict:
    """A PAYMENT-RESPONSE header back into the SettleResponse (clients read errorReason from it)."""
    return b64d_json(h)


# ---------- server side: extract what k402's engine needs ----------
def extract_payment(pp: dict) -> dict:
    """Pull the k402-relevant fields out of an x402 V2 PaymentPayload.
    Dispatch key in V2 is accepted.scheme + accepted.network (not top-level)."""
    accepted = pp.get("accepted") or {}
    payload = pp.get("payload") or {}
    extra = accepted.get("extra") or {}
    amount = accepted.get("amount")
    return {
        "scheme": accepted.get("scheme"),
        "network": accepted.get("network"),
        "amount_sompi": int(amount) if amount is not None else None,
        "pay_to": accepted.get("payTo"),
        "payment_id": extra.get("payment_id"),
        "transfer_method": extra.get("assetTransferMethod"),
        "txid": payload.get("transaction"),
        "payer": payload.get("payer"),
    }


# ---------- server side: settle / verify / supported ----------
def settle_response(*, success: bool, network: str, txid: str = "", payer: Optional[str] = None,
                    error_reason: Optional[str] = None, error_message: Optional[str] = None,
                    amount: Optional[str] = None) -> dict:
    """SettleResponse (base64'd into PAYMENT-RESPONSE). transaction is "" on failure."""
    return _prune({"success": success, "transaction": txid if success else "", "network": network,
                   "payer": payer, "amount": amount,
                   "errorReason": None if success else error_reason,
                   "errorMessage": None if success else error_message})

def encode_response_header(sr: dict) -> str:
    return b64e_json(sr)

def verify_response(*, is_valid: bool, payer: Optional[str] = None,
                    invalid_reason: Optional[str] = None, invalid_message: Optional[str] = None) -> dict:
    """VerifyResponse for a facilitator /verify."""
    return _prune({"isValid": is_valid, "payer": payer,
                   "invalidReason": None if is_valid else invalid_reason,
                   "invalidMessage": None if is_valid else invalid_message})

def supported(*, networks: list, signers: Optional[dict] = None,
              extensions: Optional[list] = None) -> dict:
    """SupportedResponse for a facilitator GET /supported."""
    return _prune({
        "kinds": [{"x402Version": X402_VERSION, "scheme": SCHEME_EXACT, "network": n, "extra": {}}
                  for n in networks],
        "extensions": extensions or [],
        "signers": signers,
    })
