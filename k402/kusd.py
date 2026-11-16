# KusdWallet: k402's handle on kUSD, as a subprocess around kusd-framework's `kusd_pay` binary (the Rust
# wallet that builds, verifies and submits kUSD transactions). Python never sees the key: the binary reads it
# from KUSD_KEY_FILE (mode 600, a regular file owned by us), named in the child's environment only. Every
# answer is one JSON object on stdout; every failure is a KusdError here.
#
# The wallet is pinned to one kUSD family (`authority`, the authority covenant id): the relay the binary reads
# is a cache, and only protocol boxes of this authority count. Verification happens on the node only.
from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from typing import Optional

# what the child may inherit besides KUSD_*: enough to run, nothing of the caller's secrets
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
              "RUST_LOG", "RUST_BACKTRACE")


class KusdError(RuntimeError):
    """kusd_pay refused, failed, or could not be run."""


@dataclass(frozen=True)
class Family:
    authority: str      # the kUSD authority covenant id: kUSD's on-chain identity (x402 `asset`)
    borrow_guard: str
    ext: str            # the family's gate commitment, part of every holder box's address
    network: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str
    index: Optional[int]
    daa: Optional[int]
    retryable: bool     # the payment may simply not be indexed yet (or the node was unreachable)


def _hex32(s: str) -> bool:
    return isinstance(s, str) and len(s) == 64 and all(c in "0123456789abcdefABCDEF" for c in s)


class KusdWallet:
    def __init__(self, bin_path: str, key_file: Optional[str] = None, *, authority: str, node_url: Optional[str] = None,
                 relay_url: Optional[str] = None, network: str = "testnet-10", timeout: float = 180.0,
                 verify_timeout: float = 60.0, pass_env: tuple = ()):
        if not _hex32(authority):
            raise KusdError("authority must be the kUSD authority covenant id (64 hex)")
        self.bin_path = bin_path
        self.pass_env = tuple(pass_env)   # extra variables the child may read from our environment (e.g. RUST_LOG)
        self.key_file = os.path.expanduser(key_file) if key_file else None
        self.authority = authority.lower()
        self.node_url = node_url
        self.relay_url = relay_url
        self.network = network
        self.timeout = timeout
        self.verify_timeout = verify_timeout
        self._family: Optional[Family] = None
        self._xonly: Optional[str] = None

    # -------- the subprocess --------
    def _check_key_file(self) -> str:
        if not self.key_file:
            raise KusdError("this operation needs the wallet key (key_file)")
        try:
            st = os.lstat(self.key_file)
        except FileNotFoundError:
            raise KusdError(f"key file {self.key_file} not found") from None
        if stat.S_ISLNK(st.st_mode):
            raise KusdError(f"{self.key_file} is a symlink; the key must be a regular file")
        if not stat.S_ISREG(st.st_mode):
            raise KusdError(f"{self.key_file} is not a regular file")
        if st.st_uid != os.getuid():
            raise KusdError(f"{self.key_file} is not owned by this user")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise KusdError(f"{self.key_file} must be mode 600 (only its owner may read the key)")
        return self.key_file

    def _env(self, needs_key: bool) -> dict:
        env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW or k in self.pass_env or k.startswith("LC_")}
        env["KUSD_NETWORK"] = self.network
        env["KUSD_AUTHORITY"] = self.authority
        if self.node_url:
            env["KUSD_NODE_URL"] = self.node_url
        if self.relay_url:
            env["KUSD_RELAY_URL"] = self.relay_url
        if needs_key:
            env["KUSD_KEY_FILE"] = self._check_key_file()
        return env

    @staticmethod
    def _parse(stdout: Optional[str]) -> Optional[dict]:
        """The last line of stdout as a JSON object, or None."""
        for line in reversed((stdout or "").strip().splitlines()):
            try:
                body = json.loads(line)
            except ValueError:
                continue
            return body if isinstance(body, dict) else None
        return None

    def _run(self, *args: str, needs_key: bool = False, timeout: Optional[float] = None) -> dict:
        env = self._env(needs_key)
        try:
            out = subprocess.run([self.bin_path, *args], env=env, capture_output=True, text=True,
                                 timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired as e:
            # the binary prints its result before publishing to the relay: a result that was printed stands
            # (a send that timed out during the publish still happened, and the caller must know its txid)
            body = self._parse(e.stdout.decode() if isinstance(e.stdout, bytes) else e.stdout)
            if body is not None and "error" not in body:
                return body
            raise KusdError(f"kusd_pay {args[0]} timed out after {timeout or self.timeout}s") from None
        except OSError as e:
            raise KusdError(f"kusd_pay could not run: {e}") from e
        body = self._parse(out.stdout)
        if out.returncode != 0 or (body is not None and "error" in body):
            raise KusdError((body or {}).get("error") or out.stderr.strip()[:300] or f"kusd_pay exited {out.returncode}")
        if body is None:
            raise KusdError(f"kusd_pay {args[0]} printed no JSON object")
        return body

    # -------- what the market needs --------
    def family(self) -> Family:
        if self._family is None:
            f = self._run("family")
            try:
                self._family = Family(authority=f["authority"], borrow_guard=f["borrow_guard"], ext=f["ext"], network=f.get("network", self.network))
            except KeyError as e:
                raise KusdError(f"kusd_pay family answered without {e}") from None
        return self._family

    def payee_xonly(self) -> str:
        """This wallet's x-only key: the listed payee key; offers are paid to it tweaked per payment_id."""
        if self._xonly is None:
            b = self.balance()
            if not _hex32(b.get("xonly", "")):
                raise KusdError("kusd_pay balance answered without the wallet key")
            self._xonly = b["xonly"]
        return self._xonly

    def send(self, to_xonly: str, cents: int) -> dict:
        return self._run("send", "--to", to_xonly, "--cents", str(int(cents)), needs_key=True)

    def verify(self, txid: str, to_xonly: str, cents: int, authority: str, since_daa: int) -> VerifyResult:
        r = self._run("verify", "--txid", txid, "--to", to_xonly, "--cents", str(int(cents)), "--authority", authority,
                      "--since-daa", str(int(since_daa)), timeout=self.verify_timeout)
        return VerifyResult(ok=bool(r.get("ok")), reason=r.get("reason", ""), index=r.get("index"), daa=r.get("daa"),
                            retryable=bool(r.get("retryable", False)) and not r.get("ok", False))

    def claim(self, txid: str, index: int, cents: int, tweak: str) -> dict:
        """Record a box paid to this wallet's key tweaked by `tweak` (an offer's), so the wallet can spend it."""
        return self._run("claim", "--txid", txid, "--index", str(int(index)), "--cents", str(int(cents)), "--tweak", tweak, needs_key=True)

    def buy(self, cents: int) -> dict:
        return self._run("buy", "--cents", str(int(cents)), needs_key=True)

    def balance(self) -> dict:
        return self._run("balance", needs_key=True)
