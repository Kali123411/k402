# HotWallet: a minimal single-address payer for the kaspa-utxo scheme.
# Meant for agent/service wallets holding small working balances — not vaults.
# Keys stay in-process and are never sent anywhere.
from __future__ import annotations

from typing import Optional

from .backend import _RpcBackend, NodeBackend, PnnBackend
from .schemes import UtxoOffer


class HotWallet:
    def __init__(self, private_key_hex: str, network: str = "mainnet",
                 backend: Optional[_RpcBackend] = None, priority_fee_sompi: int = 0):
        try:
            from kaspa import PrivateKey
        except ImportError as e:
            raise ImportError("HotWallet needs the kaspa SDK: pip install 'k402[kaspa]'") from e
        self._key = PrivateKey(private_key_hex)
        self.network = network
        self.address = self._key.to_keypair().to_address(network).to_string()
        self.backend = backend or PnnBackend(network=network)
        self.priority_fee_sompi = priority_fee_sompi

    async def balance_sompi(self) -> int:
        return await self.backend.address_received_sompi(self.address)

    async def pay(self, offer: UtxoOffer) -> str:
        """Payer protocol: pay offer.pay_to (and any facilitator fee), return txid."""
        from kaspa import PaymentOutput, Address, create_transactions

        if offer.network != self.network:
            raise ValueError(
                f"offer is for {offer.network} but this wallet is {self.network}")

        outputs = [PaymentOutput(Address(offer.pay_to), int(offer.amount_sompi))]
        if offer.facilitator_fee:
            outputs.append(PaymentOutput(
                Address(offer.facilitator_fee.to), int(offer.facilitator_fee.sompi)))

        entries = await self.backend.utxos(self.address)
        if not entries:
            raise ValueError(f"wallet {self.address} has no UTXOs to spend")

        bundle = create_transactions(
            network_id=self.network,
            entries=entries,
            outputs=outputs,
            change_address=Address(self.address),
            priority_fee=self.priority_fee_sompi,
        )
        txid = ""
        for pending in bundle["transactions"]:
            pending.sign([self._key])
            txid = await self.backend.submit_transaction(pending)
        return txid
