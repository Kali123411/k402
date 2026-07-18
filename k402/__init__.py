# k402 — HTTP 402 payments on Kaspa and Bitcoin-family UTXO chains. See PROTOCOL.md.
from .addresses import (AddressProvider, Bip32AddressProvider, CallbackAddressProvider,
                        StaticAddressProvider, XpubAddressProvider)
from .backend import (BlockbookBackend, BlockCypherBackend, ChainBackend, EsploraBackend,
                      EvmBackend, NodeBackend, PnnBackend)
from .channel import (SCHEME_CHANNEL, format_channel_header, parse_channel_header,
                      payer_pubkey_from_privkey, sign_voucher, verify_voucher,
                      voucher_digest, voucher_message)
from .channel_server import (ChannelError, ChannelManager, ChannelCovenant,
                             SubprocessChannelCovenant)
from .channel_client import ChannelOpener, ChannelPayer, SubprocessChannelOpener
from .client import Client, Payer, PaymentFailed
from .registry import CAPABILITIES, Listing, RegistryClient
from .schemes import (K402_VERSION, PAYMENT_HEADER, SESSION_HEADER,
                      BlockbookOffer, ChannelOffer, EvmOffer, FacilitatorFee, ProtocolError,
                      SessionOffer, UtxoOffer, format_payment_header, parse_offers,
                      parse_payment_header, payment_required_body)
from .server import K402, PaymentRequired
from .store import MemoryStore, PaymentRecord, PaymentStore, SqliteStore

__version__ = "0.8.0"

__all__ = [
    "K402", "Client", "PaymentRequired", "PaymentFailed",
    "UtxoOffer", "SessionOffer", "BlockbookOffer", "EvmOffer", "ChannelOffer",
    "FacilitatorFee", "ProtocolError",
    "SCHEME_CHANNEL", "voucher_message", "voucher_digest", "sign_voucher", "verify_voucher",
    "format_channel_header", "parse_channel_header", "payer_pubkey_from_privkey",
    "ChannelManager", "ChannelError", "ChannelCovenant", "SubprocessChannelCovenant",
    "Listing", "RegistryClient", "CAPABILITIES",
    "ChannelPayer", "ChannelOpener", "SubprocessChannelOpener",
    "parse_offers", "payment_required_body",
    "format_payment_header", "parse_payment_header",
    "PAYMENT_HEADER", "SESSION_HEADER", "K402_VERSION",
    "PnnBackend", "NodeBackend", "BlockbookBackend", "EsploraBackend", "BlockCypherBackend",
    "EvmBackend", "ChainBackend",
    "AddressProvider", "XpubAddressProvider", "Bip32AddressProvider",
    "CallbackAddressProvider", "StaticAddressProvider",
    "PaymentStore", "MemoryStore", "SqliteStore", "PaymentRecord",
    "Payer",
]

try:  # optional: requires the kaspa SDK extra
    from .wallet import HotWallet  # noqa: F401
    __all__.append("HotWallet")
except ImportError:
    pass
