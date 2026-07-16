# k402 — HTTP 402 payments on Kaspa and Bitcoin-family UTXO chains. See PROTOCOL.md.
from .addresses import (AddressProvider, CallbackAddressProvider,
                        StaticAddressProvider, XpubAddressProvider)
from .backend import BlockbookBackend, ChainBackend, NodeBackend, PnnBackend
from .client import Client, Payer, PaymentFailed
from .schemes import (K402_VERSION, PAYMENT_HEADER, SESSION_HEADER,
                      BlockbookOffer, FacilitatorFee, ProtocolError, SessionOffer,
                      UtxoOffer, format_payment_header, parse_offers,
                      parse_payment_header, payment_required_body)
from .server import K402, PaymentRequired
from .store import MemoryStore, PaymentRecord, PaymentStore, SqliteStore

__version__ = "0.2.0"

__all__ = [
    "K402", "Client", "PaymentRequired", "PaymentFailed",
    "UtxoOffer", "SessionOffer", "BlockbookOffer", "FacilitatorFee", "ProtocolError",
    "parse_offers", "payment_required_body",
    "format_payment_header", "parse_payment_header",
    "PAYMENT_HEADER", "SESSION_HEADER", "K402_VERSION",
    "PnnBackend", "NodeBackend", "BlockbookBackend", "ChainBackend",
    "AddressProvider", "XpubAddressProvider", "CallbackAddressProvider",
    "StaticAddressProvider",
    "PaymentStore", "MemoryStore", "SqliteStore", "PaymentRecord",
    "Payer",
]

try:  # optional: requires the kaspa SDK extra
    from .wallet import HotWallet  # noqa: F401
    __all__.append("HotWallet")
except ImportError:
    pass
