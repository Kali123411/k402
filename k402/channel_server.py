# Server side of the kaspa-channel scheme: let ANY provider accept covenant payment channels.
#
# This is the piece that makes k402 a marketplace instead of a single gateway. The channel
# settlement logic used to live in one operator's bespoke service; here it's packaged so any
# provider can `pip install k402`, hold their OWN payee key, and settle channel payments directly
# with payers — no operator, no custodian in the money path.
#
#   mgr = ChannelManager(payee_privkey=MY_KEY, backend=NodeBackend("ws://127.0.0.1:17110"),
#                        covenant=SubprocessChannelCovenant(bin_path, cwd))
#   k402 = K402(..., channel_manager=mgr)     # now every k402.paid() endpoint accepts kaspa-channel
#
# The ChannelManager verifies opens against the chain, meters per-call vouchers (pure-python
# BIP340, microseconds, no chain round-trip), and closes on-chain with the provider's own key via
# a pluggable ChannelCovenant (the tx assembler). A compromised payee key can claim at most the
# signed voucher total, to the payee's own address — consensus enforces it.
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import threading
import time
from typing import Optional, Protocol

from .channel import payer_pubkey_from_privkey, verify_voucher, voucher_message
from .schemes import ChannelOffer

# Remainder floor — a closed channel always returns >= this to the payer (0-value outputs are
# consensus-invalid and small ones blow the KIP-9 storage-mass floor). MUST match channel.sil.
FLOOR = 20_000_000


class ChannelError(Exception):
    """A channel payment couldn't be metered (bad voucher, unknown/closed channel, overdraw).
    Carries a human reason for the 402 the caller sends back."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------- covenant tx assembler (pluggable)
class ChannelCovenant(Protocol):
    """Assembles channel-covenant transactions. Implementations keep the payee key local — they
    build/sign/broadcast but never expose or transmit it."""

    def address(self, payer_pubkey: str, payee_pubkey: str, expiry_daa: int, maxfee: int) -> str:
        """The channel's P2SH address for these ctor args (used to verify an open on-chain)."""
        ...

    def close(self, *, payer_pubkey: str, payee_pubkey: str, expiry_daa: int, maxfee: int,
              chan_txid: str, chan_index: int, chan_value: int, channel_id: str, total: int,
              voucher_sig: str, payee_privkey: str) -> str:
        """Build+sign+broadcast the close with the payer's latest voucher. Returns the close txid."""
        ...


class SubprocessChannelCovenant:
    """ChannelCovenant backed by the `channel_cycle` binary — the reference assembler. Requires the
    bin on the provider's box. A pure-python or covenants-as-a-service assembler can drop in behind
    the same interface without touching ChannelManager (see PLAN-service-exchange-v1.md)."""

    def __init__(self, bin_path: str, cwd: str, network: str = "mainnet",
                 node: str = "localhost:16110"):
        self.bin_path = bin_path
        self.cwd = cwd
        self.network = network
        self.node = node

    def _run(self, env: dict, timeout: int = 90) -> str:
        out = subprocess.run([self.bin_path], cwd=self.cwd, capture_output=True, text=True,
                             timeout=timeout,
                             env={**os.environ, "NETWORK": self.network, "NODE": self.node, **env})
        if out.returncode != 0:
            raise RuntimeError(f"channel_cycle failed: {out.stderr.strip()[:200] or out.stdout.strip()[:200]}")
        return out.stdout.strip()

    def address(self, payer_pubkey, payee_pubkey, expiry_daa, maxfee):
        return self._run({"MODE": "chanaddr", "AGENT_PUBKEY": payer_pubkey,
                          "SERVICE_PUBKEY": payee_pubkey, "EXPIRY_DAA": str(expiry_daa),
                          "MAXFEE": str(maxfee)}, timeout=30).splitlines()[-1].strip()

    def close(self, *, payer_pubkey, payee_pubkey, expiry_daa, maxfee, chan_txid, chan_index,
              chan_value, channel_id, total, voucher_sig, payee_privkey):
        out = self._run({"MODE": "close", "SERVICE_KEY": payee_privkey, "AGENT_PUBKEY": payer_pubkey,
                         "SERVICE_PUBKEY": payee_pubkey, "EXPIRY_DAA": str(expiry_daa),
                         "MAXFEE": str(maxfee), "CHAN_TXID": chan_txid, "CHAN_IDX": str(chan_index),
                         "CHAN_VALUE": str(chan_value), "CHAN_COVID": channel_id, "TOTAL": str(total),
                         "VOUCHER_MSG": voucher_message(channel_id, total).hex(),
                         "VOUCHER_SIG": voucher_sig})
        if "ACCEPTED" not in out:
            raise RuntimeError(f"close not accepted: {out[-200:]}")
        return out.split("txid ")[-1].split()[0]


# ---------------------------------------------------------------- the manager
class ChannelManager:
    def __init__(self, payee_privkey: str, backend, covenant: ChannelCovenant, *,
                 network: str = "mainnet",
                 min_channel: int = 100_000_000, max_channel: int = 500_000_000,
                 maxfee: int = 5_000_000, min_expiry_delta: int = 864_000,
                 close_threshold: int = 100_000_000, close_margin_daa: int = 72_000,
                 store_path: Optional[str] = None):
        self.payee_privkey = payee_privkey
        self.payee_pubkey = payer_pubkey_from_privkey(payee_privkey)
        self.backend = backend
        self.covenant = covenant
        self.network = network
        self.min_channel = min_channel
        self.max_channel = max_channel
        self.maxfee = maxfee
        self.min_expiry_delta = min_expiry_delta
        self.close_threshold = close_threshold
        self.close_margin_daa = close_margin_daa
        self._store_path = pathlib.Path(store_path) if store_path else None
        self._lock = threading.Lock()
        self._chans: dict = self._load()

    # -------- persistence (optional json file; in-memory otherwise) --------
    def _load(self) -> dict:
        if self._store_path and self._store_path.exists():
            try:
                return json.loads(self._store_path.read_text()).get("channels", {})
            except Exception:
                pass
        return {}

    def _save(self):
        if self._store_path:
            self._store_path.write_text(json.dumps({"channels": self._chans}, indent=1))
            try:
                self._store_path.chmod(0o600)
            except OSError:
                pass

    # -------- the offer --------
    def offer(self, price_sompi: int, open_url: str = "/channel/open") -> ChannelOffer:
        return ChannelOffer(
            network=self.network, payee_pubkey=self.payee_pubkey, price_sompi=str(price_sompi),
            min_channel_sompi=str(self.min_channel), max_channel_sompi=str(self.max_channel),
            min_expiry_daa_delta=self.min_expiry_delta, maxfee_sompi=str(self.maxfee), open=open_url,
            description="covenant payment channel — settle per-call vouchers on Kaspa L1, no custodian")

    # -------- open verification (against the chain) --------
    async def verify_open(self, payer_pubkey: str, expiry_daa: int, txid: str, index: int = 0) -> dict:
        """Register a funded channel after checking everything against the node: the recompiled
        P2SH address holds the outpoint, value is in bounds, it carries a covenant id, expiry sane.
        Nothing is taken on trust. Returns a summary; raises ValueError on any failure."""
        payer_pubkey, txid = payer_pubkey.lower(), txid.lower()
        if len(bytes.fromhex(payer_pubkey)) != 32:
            raise ValueError("payer_pubkey must be 32-byte x-only hex")
        try:
            daa = await self.backend.daa_score()
        except Exception as e:
            raise ValueError(f"node unreachable: {type(e).__name__}")
        if expiry_daa < daa + self.min_expiry_delta:
            raise ValueError(f"expiry_daa too soon: need >= {daa + self.min_expiry_delta}")
        addr = self.covenant.address(payer_pubkey, self.payee_pubkey, expiry_daa, self.maxfee)
        for e in await self.backend.utxos(addr):
            op = e.get("outpoint", {})
            if str(op.get("transactionId", op.get("transaction_id", ""))).lower() != txid \
                    or int(op.get("index", -1)) != index:
                continue
            ue = e.get("utxoEntry", e.get("utxo_entry", {}))
            value = int(ue.get("amount", 0))
            raw = ue.get("covenantId", ue.get("covenant_id", ""))
            covid = (raw.hex() if isinstance(raw, (bytes, bytearray)) else str(raw or "")).lower()
            if not covid:
                raise ValueError("outpoint has no covenant id — not opened as a channel genesis box")
            if not (self.min_channel <= value <= self.max_channel):
                raise ValueError(f"channel value {value} outside [{self.min_channel}, {self.max_channel}]")
            with self._lock:
                if covid in self._chans:
                    raise ValueError("channel already registered")
                self._chans[covid] = {"payer": payer_pubkey, "expiry": expiry_daa,
                                      "maxfee": self.maxfee, "value": value, "addr": addr,
                                      "txid": txid, "index": index, "total": 0, "voucher": "",
                                      "closed": None, "created": int(time.time())}
                self._save()
            return {"channel": covid, "value_sompi": value, "expiry_daa": expiry_daa,
                    "spendable_sompi": value - self.maxfee - FLOOR}
        raise ValueError(f"outpoint {txid}:{index} not found at channel address {addr} "
                         "(unconfirmed, wrong params, or already spent)")

    # -------- per-call metering (the hot path — pure python, no chain) --------
    def charge(self, channel_id: str, price: int, total: int, voucher: str) -> dict:
        """Meter one call: verify the voucher and advance the cumulative total. Raises ChannelError
        (which the server turns into a 402) on any problem. Microseconds; no chain round-trip."""
        cid = channel_id.lower()
        with self._lock:
            c = self._chans.get(cid)
            if c is None:
                raise ChannelError("unknown channel — register at /channel/open")
            if c["closed"]:
                raise ChannelError("channel closed")
            ceiling = c["value"] - c["maxfee"] - FLOOR
            if total > ceiling:
                raise ChannelError(f"total {total} exceeds channel ceiling {ceiling} — open a new channel")
            if total - c["total"] < price:
                raise ChannelError(f"voucher total must advance by >= {price} (have {c['total']}, got {total})")
            if not verify_voucher(c["payer"], cid, total, voucher):
                raise ChannelError("voucher signature invalid")
            c["total"], c["voucher"] = total, voucher
            self._save()
            return {"ok": True, "charged": price, "channel_total": total,
                    "remaining_sompi": ceiling - total}

    def status(self, channel_id: str) -> Optional[dict]:
        with self._lock:
            c = self._chans.get(channel_id.lower())
            if not c:
                return None
            ceiling = c["value"] - c["maxfee"] - FLOOR
            return {"channel": channel_id, "value_sompi": c["value"], "spent_sompi": c["total"],
                    "remaining_sompi": ceiling - c["total"], "expiry_daa": c["expiry"],
                    "closed": bool(c["closed"]), "close_txid": c["closed"]}

    # -------- closing (on-chain, provider's own key) --------
    def close(self, channel_id: str) -> str:
        """Close on-chain with the latest voucher; consensus enforces the split. Returns the txid."""
        cid = channel_id.lower()
        with self._lock:
            c = self._chans.get(cid)
            if not c:
                raise ValueError("unknown channel")
            if c["closed"]:
                return c["closed"]
            if c["total"] <= 0:
                raise ValueError("nothing to claim (no vouchers)")
            snap = dict(c)
        txid = self.covenant.close(
            payer_pubkey=snap["payer"], payee_pubkey=self.payee_pubkey, expiry_daa=snap["expiry"],
            maxfee=snap["maxfee"], chan_txid=snap["txid"], chan_index=snap["index"],
            chan_value=snap["value"], channel_id=cid, total=snap["total"],
            voucher_sig=snap["voucher"], payee_privkey=self.payee_privkey)
        with self._lock:
            self._chans[cid]["closed"] = txid
            self._save()
        return txid

    async def maybe_close(self) -> list:
        """Close channels whose claimable value crosses the threshold or whose expiry nears.
        Call this from a periodic worker. Returns [(channel_id, txid_or_error), ...]."""
        with self._lock:
            candidates = {k: dict(v) for k, v in self._chans.items()
                          if not v["closed"] and v["total"] > 0}
        if not candidates:
            return []
        try:
            daa = await self.backend.daa_score()
        except Exception:
            return []
        results = []
        for cid, c in candidates.items():
            if c["total"] >= self.close_threshold or daa >= c["expiry"] - self.close_margin_daa:
                try:
                    results.append((cid, self.close(cid)))
                except Exception as e:
                    results.append((cid, f"error: {type(e).__name__}: {e}"))
        return results
