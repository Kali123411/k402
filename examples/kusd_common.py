# Shared wiring for the kUSD market demo (TN10): the kUSD wallet from env, a chain backend for the KAS side
# of each 402, and the k402 payment store. Both runners (kusd_registry.py, kusd_provider.py) import this.
#
# Env: KUSD_PAY_BIN, KUSD_KEY_FILE (mode 600), KUSD_AUTHORITY (pins the family), KUSD_RELAY_URL, KUSD_NODE_URL,
#      KUSD_NETWORK (testnet-10); KASPAD_GRPC (host:port of a TN10 node's gRPC; needs the kaspa_grpc helper on
#      PYTHONPATH) or else the public TN10 resolver (kaspa SDK); K402_STORE (sqlite path for payment records).
import os

from k402 import K402, KusdWallet, SqliteStore
from k402.addresses import StaticAddressProvider

NETWORK = os.environ.get("KUSD_NETWORK", "testnet-10")


def wallet() -> KusdWallet:
    return KusdWallet(os.environ["KUSD_PAY_BIN"], key_file=os.path.expanduser(os.environ["KUSD_KEY_FILE"]),
                      authority=os.environ["KUSD_AUTHORITY"], node_url=os.environ.get("KUSD_NODE_URL"),
                      relay_url=os.environ.get("KUSD_RELAY_URL"), network=NETWORK)


class GrpcBackend:
    """k402 ChainBackend over a node's gRPC (balance, utxos, daa_score), shaped like the SDK dicts."""

    def __init__(self):
        import kaspa_grpc as node
        from kaspa_grpc import rpcpb as pb
        self.node, self.pb = node, pb

    async def address_received_sompi(self, address: str) -> int:
        return int(self.node.call(getBalanceByAddressRequest=self.pb.GetBalanceByAddressRequestMessage(address=address)).balance)

    async def daa_score(self) -> int:
        return int(self.node.call(getBlockDagInfoRequest=self.pb.GetBlockDagInfoRequestMessage()).virtualDaaScore)

    async def utxos(self, address: str) -> list:
        m = self.node.call(getUtxosByAddressesRequest=self.pb.GetUtxosByAddressesRequestMessage(addresses=[address]))
        return [{"outpoint": {"transactionId": e.outpoint.transactionId, "index": e.outpoint.index},
                 "utxoEntry": {"amount": e.utxoEntry.amount}} for e in m.entries]

    async def close(self) -> None:
        pass


def backend():
    if os.environ.get("KASPAD_GRPC"):
        return GrpcBackend()
    from k402 import PnnBackend
    return PnnBackend(network=NETWORK)


def k402_for(w: KusdWallet, be, *, kusd_price_cents: int, kas_price_sompi: int = 0) -> K402:
    """A K402 that offers kUSD (and KAS to this wallet's own address) and remembers payments in sqlite."""
    kas_address = w.balance()["address"]
    return K402(address_provider=StaticAddressProvider(kas_address), backend=be, network=NETWORK,
                store=SqliteStore(os.path.expanduser(os.environ.get("K402_STORE", "~/.kusd-market/k402-store.db"))),
                quote_ttl=int(os.environ.get("QUOTE_TTL", "180")), kusd=w, kusd_price_cents=kusd_price_cents,
                min_payable_sompi=kas_price_sompi or None)
