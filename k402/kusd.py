# k402/kusd.py — the kUSD wallet behind the kUSD market (DESIGN-kusd-market.md §2): a thin wrapper over the
# `kusd_pay` binary from kusd-framework, run as a subprocess so the key never enters this process. JSON in,
# JSON out; the secret is read by the binary from a mode-600 file named in its environment, never argv.
from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from typing import Optional


class KusdError(RuntimeError):
    """kusd_pay refused or failed (its reason), or could not be run safely."""


@dataclass(frozen=True)
class Family:
    """The kUSD family: the authority's covenant id (kUSD's on-chain identity, the x402 asset) and the two
    state constants every holder box of the family carries."""
    authority: str
    borrow_guard: str
    ext: str
    network: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str
    index: Optional[int]
    daa: Optional[int]
    retryable: bool   # the payment is not indexed yet at the expected box: ask again shortly


class KusdWallet:
    def __init__(self, bin_path: str, key_file: Optional[str] = None, *, authority: str, node_url: Optional[str] = None,
                 relay_url: Optional[str] = None, network: str = "testnet-10", timeout: float = 180.0):
        # `authority` pins the kUSD family (the authority covenant id): the relay the binary reads is a cache,
        # and only protocol boxes of this authority count. Verification is on the node only.
        if len(authority) != 64 or not all(c in "0123456789abcdefABCDEF" for c in authority):
            raise KusdError("authority must be the kUSD authority covenant id (64 hex)")
        self.bin_path = bin_path
        self.key_file = key_file
        self.authority = authority.lower()
        self.node_url = node_url
        self.relay_url = relay_url
        self.network = network
        self.timeout = timeout
        self._family: Optional[Family] = None
        self._xonly: Optional[str] = None

    # -------- the subprocess --------
    def _env(self, needs_key: bool) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("KUSD_")}
        env["KUSD_NETWORK"] = self.network
        env["KUSD_AUTHORITY"] = self.authority
        if self.node_url:
            env["KUSD_NODE_URL"] = self.node_url
        if self.relay_url:
            env["KUSD_RELAY_URL"] = self.relay_url
        if needs_key:
            if not self.key_file:
                raise KusdError("this operation needs the wallet key (key_file)")
            mode = stat.S_IMODE(os.stat(self.key_file).st_mode)
            if mode & 0o077:
                raise KusdError(f"{self.key_file} must be mode 600 (only its owner may read the key)")
            env["KUSD_KEY_FILE"] = self.key_file
        return env

    def _run(self, *args: str, needs_key: bool = False) -> dict:
        env = self._env(needs_key)
        try:
            out = subprocess.run([self.bin_path, *args], env=env, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise KusdError(f"kusd_pay could not run: {e}") from e
        try:
            body = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
        except (ValueError, IndexError):
            body = {}
        if out.returncode != 0 or "error" in body:
            raise KusdError(body.get("error") or out.stderr.strip()[:300] or f"kusd_pay exited {out.returncode}")
        return body

    # -------- the four things the market needs --------
    def family(self) -> Family:
        if self._family is None:
            f = self._run("family")
            self._family = Family(authority=f["authority"], borrow_guard=f["borrow_guard"], ext=f["ext"], network=f.get("network", self.network))
        return self._family

    def payee_xonly(self) -> str:
        """This wallet's x-only key: the kUSD box owner a payer pays, and the k402 payee key."""
        if self._xonly is None:
            self._xonly = self.balance()["xonly"]
        return self._xonly

    def send(self, to_xonly: str, cents: int) -> dict:
        return self._run("send", "--to", to_xonly, "--cents", str(int(cents)), needs_key=True)

    def verify(self, txid: str, to_xonly: str, cents: int, authority: str, since_daa: int) -> VerifyResult:
        r = self._run("verify", "--txid", txid, "--to", to_xonly, "--cents", str(int(cents)), "--authority", authority, "--since-daa", str(int(since_daa)))
        return VerifyResult(ok=bool(r.get("ok")), reason=r.get("reason", ""), index=r.get("index"), daa=r.get("daa"),
                            retryable=bool(r.get("retryable", False)) and not r.get("ok", False))

    def buy(self, cents: int) -> dict:
        return self._run("buy", "--cents", str(int(cents)), needs_key=True)

    def balance(self) -> dict:
        return self._run("balance", needs_key=True)
