# Address providers hand the server a fresh pay_to address per payment_id.
# The recommended provider is watch-only xpub derivation: the web server never
# holds a private key, so a compromised server can lose at most unswept revenue.
from __future__ import annotations

import itertools
import threading
from typing import Callable, Protocol


class AddressProvider(Protocol):
    def next_address(self, payment_id: str) -> str: ...


class CallbackAddressProvider:
    """Bring your own derivation: fn(payment_id) -> address."""

    def __init__(self, fn: Callable[[str], str]):
        self._fn = fn

    def next_address(self, payment_id: str) -> str:
        return self._fn(payment_id)


class XpubAddressProvider:
    """Watch-only HD derivation from an account xpub (kaspa SDK required).

    NOTE: the index counter is in-memory. Across restarts pass start_index
    higher than any previously issued index (persist it next to your payment
    store) — reusing an index only risks correlating two payments to one
    address, not losing funds, but fresh-per-payment is the protocol's intent.
    """

    def __init__(self, xpub: str, network: str = "mainnet", start_index: int = 0):
        try:
            from kaspa import PublicKeyGenerator
        except ImportError as e:
            raise ImportError(
                "XpubAddressProvider needs the kaspa SDK: pip install 'k402[kaspa]'") from e
        self._gen = PublicKeyGenerator.from_xpub(xpub)
        self._network = network
        self._counter = itertools.count(start_index)
        self._lock = threading.Lock()

    def next_address(self, payment_id: str) -> str:
        with self._lock:
            index = next(self._counter)
        return self._gen.receive_address_as_string(self._network, index)


class StaticAddressProvider:
    """A fixed address for every payment. Dev/demo ONLY: concurrent payments to
    one address can satisfy each other's verification. Never use in production."""

    def __init__(self, address: str):
        self._address = address

    def next_address(self, payment_id: str) -> str:
        return self._address


# --- Bitcoin-family fresh-address derivation from an account xpub (watch-only) --------------------
# One provider covers BTC, LTC, DOGE, BCH, DASH, ZEC-transparent, BSV: derive account/0/index and
# encode per coin. The wallet exports a standard `xpub` (0488b21e) even for segwit coins, so we
# derive the raw key with generic BIP32 and pick the address encoding by coin. Requires `bip-utils`
# (pip install 'k402[btc]'). VERIFY index 0 matches your wallet's first receive address before use.
def _bip_encoders():
    from bip_utils import P2WPKHAddrEncoder, P2PKHAddrEncoder, BchP2PKHAddrEncoder  # noqa: F401
    # coin -> callable(pubkey_key_object) -> address string
    return {
        "bitcoin":      lambda k: P2WPKHAddrEncoder.EncodeKey(k, hrp="bc"),
        "litecoin":     lambda k: P2WPKHAddrEncoder.EncodeKey(k, hrp="ltc"),
        "dogecoin":     lambda k: P2PKHAddrEncoder.EncodeKey(k, net_ver=b"\x1e"),
        "dash":         lambda k: P2PKHAddrEncoder.EncodeKey(k, net_ver=b"\x4c"),
        "bitcoin-sv":   lambda k: P2PKHAddrEncoder.EncodeKey(k, net_ver=b"\x00"),
        "zcash":        lambda k: P2PKHAddrEncoder.EncodeKey(k, net_ver=b"\x1c\xb8"),
        "bitcoin-cash": lambda k: BchP2PKHAddrEncoder.EncodeKey(k, hrp="bitcoincash", net_ver=b"\x00"),
    }


class Bip32AddressProvider:
    """Fresh Bitcoin-family address per payment, HD-derived from a watch-only account xpub.
    Eliminates the reused-address concurrency caveat (each offer gets its own address). The index
    is persisted in a sqlite db so restarts never reuse an address.

    coin: one of bitcoin, litecoin, dogecoin, dash, bitcoin-sv, zcash, bitcoin-cash.
    """

    def __init__(self, xpub: str, coin: str, db_path: str, start_index: int = 0):
        try:
            from bip_utils import Bip32Slip10Secp256k1  # noqa: F401
        except ImportError as e:
            raise ImportError("Bip32AddressProvider needs bip-utils: pip install 'k402[btc]'") from e
        import sqlite3
        from bip_utils import Bip32Slip10Secp256k1
        encoders = _bip_encoders()
        if coin not in encoders:
            raise ValueError(f"unsupported coin '{coin}' — one of {sorted(encoders)}")
        self._node = Bip32Slip10Secp256k1.FromExtendedKey(xpub)
        self._encode = encoders[coin]
        self._db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS k402_addr_index "
                         "(id INTEGER PRIMARY KEY CHECK (id=0), next INTEGER)")
        self._db.execute("INSERT OR IGNORE INTO k402_addr_index VALUES (0, ?)", (start_index,))
        self._lock = threading.Lock()

    def address_at(self, index: int) -> str:
        """Derive a specific receive index (account/0/index). Use to VERIFY index 0 matches
        your wallet before relying on the provider."""
        return self._encode(self._node.DerivePath(f"0/{index}").PublicKey().KeyObject())

    def next_address(self, payment_id: str) -> str:
        with self._lock:
            row = self._db.execute(
                "UPDATE k402_addr_index SET next = next + 1 RETURNING next - 1").fetchone()
        return self.address_at(row[0])
