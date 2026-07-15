# Wire format for the k402 protocol: 402 offer bodies and payment headers.
# This module is dependency-free and is the normative reference implementation
# of PROTOCOL.md — keep the two in lockstep.
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Optional

K402_VERSION = "0.1"
PAYMENT_HEADER = "X-K402-Payment"
SESSION_HEADER = "X-Session"

SCHEME_UTXO = "kaspa-utxo"
SCHEME_SESSION = "kaspa-session"


class ProtocolError(ValueError):
    """Malformed k402 wire data (offer body or payment header)."""


def new_payment_id() -> str:
    return "p_" + secrets.token_hex(8)


@dataclass
class FacilitatorFee:
    """Optional, transparent service fee quoted inside an offer (never a rail toll)."""
    sompi: str
    to: str
    by: str = ""

    def to_dict(self) -> dict:
        d = {"sompi": self.sompi, "to": self.to}
        if self.by:
            d["by"] = self.by
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FacilitatorFee":
        return cls(sompi=str(d["sompi"]), to=d["to"], by=d.get("by", ""))


@dataclass
class UtxoOffer:
    """kaspa-utxo: non-custodial per-call payment to a fresh address."""
    network: str
    amount_sompi: str
    pay_to: str
    payment_id: str
    expires: int
    description: str = ""
    finality: int = 1
    facilitator_fee: Optional[FacilitatorFee] = None
    scheme: str = field(default=SCHEME_UTXO, init=False)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "scheme": self.scheme,
            "network": self.network,
            "amount_sompi": str(self.amount_sompi),
            "pay_to": self.pay_to,
            "payment_id": self.payment_id,
            "expires": self.expires,
        }
        if self.description:
            d["description"] = self.description
        if self.finality != 1:
            d["finality"] = self.finality
        if self.facilitator_fee:
            d["facilitator_fee"] = self.facilitator_fee.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "UtxoOffer":
        try:
            amount = str(d["amount_sompi"])
            int(amount)  # must be an integer string; float KAS never crosses the wire
            return cls(
                network=d["network"],
                amount_sompi=amount,
                pay_to=d["pay_to"],
                payment_id=d["payment_id"],
                expires=int(d["expires"]),
                description=d.get("description", ""),
                finality=int(d.get("finality", 1)),
                facilitator_fee=FacilitatorFee.from_dict(d["facilitator_fee"])
                if d.get("facilitator_fee") else None,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProtocolError(f"invalid kaspa-utxo offer: {e}") from e

    @property
    def total_sompi(self) -> int:
        """Amount the payer sends in total, including any facilitator fee output."""
        total = int(self.amount_sompi)
        if self.facilitator_fee:
            total += int(self.facilitator_fee.sompi)
        return total


@dataclass
class SessionOffer:
    """kaspa-session: prepaid metered balance (deposit address minted by `open`)."""
    open: str
    scheme: str = field(default=SCHEME_SESSION, init=False)

    def to_dict(self) -> dict:
        return {"scheme": self.scheme, "open": self.open}

    @classmethod
    def from_dict(cls, d: dict) -> "SessionOffer":
        try:
            return cls(open=d["open"])
        except KeyError as e:
            raise ProtocolError(f"invalid kaspa-session offer: {e}") from e


Offer = UtxoOffer | SessionOffer

_SCHEME_TYPES = {SCHEME_UTXO: UtxoOffer, SCHEME_SESSION: SessionOffer}


def payment_required_body(offers: list[Offer]) -> dict:
    """The JSON body of a k402 HTTP 402 response."""
    return {"k402": K402_VERSION, "accepts": [o.to_dict() for o in offers]}


def parse_offers(body: dict) -> list[Offer]:
    """Parse a 402 body; unknown schemes are skipped (forward compatibility)."""
    if not isinstance(body, dict) or "k402" not in body:
        raise ProtocolError("not a k402 402 body (missing 'k402' version key)")
    offers: list[Offer] = []
    for entry in body.get("accepts", []):
        typ = _SCHEME_TYPES.get(entry.get("scheme"))
        if typ is not None:
            offers.append(typ.from_dict(entry))
    return offers


def format_payment_header(txid: str, payment_id: str, scheme: str = SCHEME_UTXO) -> str:
    return f"{scheme} {txid} {payment_id}"


def parse_payment_header(value: str) -> tuple[str, str, str]:
    """-> (scheme, txid, payment_id)"""
    parts = value.strip().split()
    if len(parts) != 3:
        raise ProtocolError(
            f"malformed {PAYMENT_HEADER} header (want '<scheme> <txid> <payment_id>')")
    return parts[0], parts[1], parts[2]
