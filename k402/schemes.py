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
SCHEME_BLOCKBOOK = "blockbook-utxo"
SCHEME_EVM = "evm"
SCHEME_CHANNEL = "kaspa-channel"


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


@dataclass
class BlockbookOffer:
    """blockbook-utxo: non-custodial per-call payment on any Bitcoin-family UTXO chain served by a
    Blockbook indexer (Bitcoin, Litecoin, Dogecoin, Bitcoin Cash, Dash, transparent Zcash, Pearl…).
    `coin` names the chain; `decimals` lets a client render the amount. Verification is the same as
    kaspa-utxo — did `pay_to` receive >= amount — answered by Blockbook's address endpoint."""
    coin: str
    network: str
    amount: str
    decimals: int
    pay_to: str
    payment_id: str
    expires: int
    description: str = ""
    finality: int = 1
    facilitator_fee: Optional[FacilitatorFee] = None
    scheme: str = field(default=SCHEME_BLOCKBOOK, init=False)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "scheme": self.scheme, "coin": self.coin, "network": self.network,
            "amount": str(self.amount), "decimals": self.decimals,
            "pay_to": self.pay_to, "payment_id": self.payment_id, "expires": self.expires,
        }
        if self.description:
            d["description"] = self.description
        if self.finality != 1:
            d["finality"] = self.finality
        if self.facilitator_fee:
            d["facilitator_fee"] = self.facilitator_fee.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BlockbookOffer":
        try:
            amount = str(d["amount"])
            int(amount)  # atomic units, integer string
            return cls(
                coin=d["coin"], network=d["network"], amount=amount,
                decimals=int(d["decimals"]), pay_to=d["pay_to"], payment_id=d["payment_id"],
                expires=int(d["expires"]), description=d.get("description", ""),
                finality=int(d.get("finality", 1)),
                facilitator_fee=FacilitatorFee.from_dict(d["facilitator_fee"])
                if d.get("facilitator_fee") else None,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProtocolError(f"invalid blockbook-utxo offer: {e}") from e

    @property
    def total_atomic(self) -> int:
        total = int(self.amount)
        if self.facilitator_fee:
            total += int(self.facilitator_fee.sompi)
        return total


@dataclass
class EvmOffer:
    """evm: per-call payment on any EVM chain (Ethereum Classic, Ethereum, L2s…) in the native coin
    or an ERC-20 token. `chain_id` identifies the network; `token` is None for the native coin or an
    ERC-20 contract address. Verification is a balance delta since the offer (see PROTOCOL.md §5):
    the merchant reads eth_getBalance (native) or balanceOf (token) for pay_to."""
    chain: str
    chain_id: int
    asset: str
    amount: str          # wei / token base units, integer string
    decimals: int
    pay_to: str          # 0x address
    payment_id: str
    expires: int
    token: Optional[str] = None   # ERC-20 contract; None = native coin
    description: str = ""
    finality: int = 1
    facilitator_fee: Optional[FacilitatorFee] = None
    scheme: str = field(default=SCHEME_EVM, init=False)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "scheme": self.scheme, "chain": self.chain, "chain_id": self.chain_id,
            "asset": self.asset, "amount": str(self.amount), "decimals": self.decimals,
            "pay_to": self.pay_to, "payment_id": self.payment_id, "expires": self.expires,
        }
        if self.token:
            d["token"] = self.token
        if self.description:
            d["description"] = self.description
        if self.finality != 1:
            d["finality"] = self.finality
        if self.facilitator_fee:
            d["facilitator_fee"] = self.facilitator_fee.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EvmOffer":
        try:
            amount = str(d["amount"])
            int(amount)  # base units, integer string
            return cls(
                chain=d["chain"], chain_id=int(d["chain_id"]), asset=d["asset"], amount=amount,
                decimals=int(d["decimals"]), pay_to=d["pay_to"], payment_id=d["payment_id"],
                expires=int(d["expires"]), token=d.get("token"),
                description=d.get("description", ""), finality=int(d.get("finality", 1)),
                facilitator_fee=FacilitatorFee.from_dict(d["facilitator_fee"])
                if d.get("facilitator_fee") else None,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProtocolError(f"invalid evm offer: {e}") from e

    @property
    def total_atomic(self) -> int:
        total = int(self.amount)
        if self.facilitator_fee:
            total += int(self.facilitator_fee.sompi)
        return total


@dataclass
class ChannelOffer:
    """kaspa-channel: covenant-enforced unidirectional payment channel (PROTOCOL.md §4, 0.2).
    The payer compiles the channel covenant with ctor args (its pubkey, payee_pubkey, an expiry
    >= now + min_expiry_daa_delta, maxfee_sompi), funds it on `network` within the min/max bounds,
    and registers the outpoint at `open`. Per call it signs a voucher over the cumulative total
    (see k402.channel); the merchant verifies off-chain and can close on-chain with the latest."""
    network: str
    payee_pubkey: str            # x-only hex — the covenant's payee ctor arg
    price_sompi: str             # per-call price metered against the channel
    min_channel_sompi: str
    max_channel_sompi: str
    min_expiry_daa_delta: int    # payer must set expiry at least this far past the current DAA
    maxfee_sompi: str            # the covenant's maxFee ctor arg (both sides must agree)
    open: str                    # registration URL for a funded channel outpoint
    description: str = ""
    scheme: str = field(default=SCHEME_CHANNEL, init=False)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "scheme": self.scheme, "network": self.network, "payee_pubkey": self.payee_pubkey,
            "price_sompi": str(self.price_sompi), "min_channel_sompi": str(self.min_channel_sompi),
            "max_channel_sompi": str(self.max_channel_sompi),
            "min_expiry_daa_delta": self.min_expiry_daa_delta,
            "maxfee_sompi": str(self.maxfee_sompi), "open": self.open,
        }
        if self.description:
            d["description"] = self.description
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelOffer":
        try:
            for k in ("price_sompi", "min_channel_sompi", "max_channel_sompi", "maxfee_sompi"):
                int(str(d[k]))  # integer strings, never float KAS
            return cls(
                network=d["network"], payee_pubkey=d["payee_pubkey"],
                price_sompi=str(d["price_sompi"]), min_channel_sompi=str(d["min_channel_sompi"]),
                max_channel_sompi=str(d["max_channel_sompi"]),
                min_expiry_daa_delta=int(d["min_expiry_daa_delta"]),
                maxfee_sompi=str(d["maxfee_sompi"]), open=d["open"],
                description=d.get("description", ""),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProtocolError(f"invalid kaspa-channel offer: {e}") from e


Offer = UtxoOffer | SessionOffer | BlockbookOffer | EvmOffer | ChannelOffer

_SCHEME_TYPES = {SCHEME_UTXO: UtxoOffer, SCHEME_SESSION: SessionOffer,
                 SCHEME_BLOCKBOOK: BlockbookOffer, SCHEME_EVM: EvmOffer,
                 SCHEME_CHANNEL: ChannelOffer}


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
