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
