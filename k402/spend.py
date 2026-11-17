# Operator spending controls for a kUSD-market agent (DESIGN-kusd-spend-controls.md).
#
# The person who funds an agent's wallet writes a SpendPolicy; KusdPayer measures every payment against it
# through a SpendLedger (sqlite) before any money moves. The policy can only tighten what the agent asks for,
# and the agent never gets a tool that edits it: that is the boundary between a wallet and an allowance.
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, fields
from typing import Optional

from .kusd import KusdError

_HEX32 = re.compile(r"^[0-9a-f]{64}$")

# why a payment was refused; each maps to one bound (or the kill switch)
REFUSAL_CODES = frozenset({"frozen", "per_call", "total", "window_cents", "window_calls", "topup", "failures",
                           "payee", "unlisted_payee", "capability"})

_INT_FIELDS = ("max_per_call_cents", "max_total_cents", "max_window_cents", "max_window_calls",
               "max_window_topup_cents", "max_window_failures")


class SpendRefused(KusdError):
    """A payment the policy did not allow. `code` names the bound; `status` is the ledger's view at the time."""

    def __init__(self, code: str, reason: str, status: dict):
        if code not in REFUSAL_CODES:
            raise ValueError(f"unknown refusal code {code!r}")
        self.code, self.reason, self.status = code, reason, dict(status)
        super().__init__(f"spend refused ({code}): {reason}")


@dataclass(frozen=True)
class SpendPolicy:
    """The operator's bounds. Every limit is optional (None = unbounded on that axis); the policy tightens
    an agent's own cap, never loosens it."""
    max_per_call_cents: Optional[int] = None
    max_total_cents: Optional[int] = None            # lifetime of the ledger
    window_seconds: int = 3600
    max_window_cents: Optional[int] = None           # kUSD sent per window
    max_window_calls: Optional[int] = None           # paid calls per window
    max_window_topup_cents: Optional[int] = None     # kUSD bought through the PSM per window; 0 = never
    max_window_failures: Optional[int] = None        # paid-but-unserved calls per window before stopping
    allow_payees: Optional[frozenset] = None         # listed provider keys (x-only hex) the agent may pay
    allow_capabilities: Optional[frozenset] = None
    require_listed_payee: bool = True

    def __post_init__(self):
        for name in _INT_FIELDS:
            v = getattr(self, name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                raise ValueError(f"{name} must be a non-negative integer or null, got {v!r}")
        if isinstance(self.window_seconds, bool) or not isinstance(self.window_seconds, int) or self.window_seconds <= 0:
            raise ValueError(f"window_seconds must be a positive integer, got {self.window_seconds!r}")
        if self.allow_payees is not None:
            keys = frozenset(str(k).lower() for k in self.allow_payees)
            bad = [k for k in keys if not _HEX32.match(k)]
            if bad:
                raise ValueError(f"allow_payees must be 32-byte x-only keys in hex, got {bad[0]!r}")
            object.__setattr__(self, "allow_payees", keys)
        if self.allow_capabilities is not None:
            object.__setattr__(self, "allow_capabilities", frozenset(str(c) for c in self.allow_capabilities))
        if not isinstance(self.require_listed_payee, bool):
            raise ValueError("require_listed_payee must be true or false")

    def cap(self, agent_cap: Optional[int]) -> Optional[int]:
        """The price ceiling for one call: the tighter of the agent's and the policy's."""
        caps = [c for c in (agent_cap, self.max_per_call_cents) if c is not None]
        return min(caps) if caps else None

    @classmethod
    def from_dict(cls, d: dict) -> "SpendPolicy":
        if not isinstance(d, dict):
            raise ValueError("a spend policy is a JSON object")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(d) - known)
        if unknown:
            raise ValueError(f"unknown spend policy field(s): {', '.join(unknown)}")
        kw = dict(d)
        for name in ("allow_payees", "allow_capabilities"):
            if kw.get(name) is not None:
                if not isinstance(kw[name], (list, tuple, set, frozenset)):
                    raise ValueError(f"{name} must be a list")
                kw[name] = frozenset(kw[name])
        return cls(**kw)

    def to_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        for name in ("allow_payees", "allow_capabilities"):
            if d[name] is not None:
                d[name] = sorted(d[name])
        return d

    @classmethod
    def load(cls, path: str) -> "SpendPolicy":
        """Read the operator's policy from a JSON file (`~` expanded)."""
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


_KINDS = ("pay", "topup")


class SpendLedger:
    """Every spend the payer made or reserved, in sqlite (`":memory:"` for tests). The bounds are measured
    against this, never against process memory, so a restart cannot reset a budget. One connection, one lock,
    and `reserve` runs its checks and its insert in a single BEGIN IMMEDIATE transaction, so two concurrent
    payments cannot both pass the same remaining budget."""

    def __init__(self, path: str):
        path = path if path == ":memory:" else os.path.expanduser(path)
        self.path = path
        self._lock = threading.Lock()
        self._c = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._c.row_factory = sqlite3.Row
        with self._lock:
            self._c.execute("PRAGMA busy_timeout=30000")
            self._c.execute("""CREATE TABLE IF NOT EXISTS spend (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '', capability TEXT NOT NULL DEFAULT '',
                cents INTEGER NOT NULL, kas_sompi INTEGER NOT NULL DEFAULT 0,
                txid TEXT NOT NULL DEFAULT '', http INTEGER, state TEXT NOT NULL)""")
            self._c.execute("CREATE INDEX IF NOT EXISTS spend_ts ON spend(ts)")
            self._c.execute("CREATE TABLE IF NOT EXISTS spend_control (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def close(self) -> None:
        with self._lock:
            self._c.close()

    # -------- the kill switch --------
    @property
    def frozen(self) -> bool:
        with self._lock:
            return self._frozen()

    def _frozen(self) -> bool:
        r = self._c.execute("SELECT value FROM spend_control WHERE key='frozen'").fetchone()
        return bool(r) and r["value"] == "1"

    def freeze(self) -> None:
        with self._lock:
            self._c.execute("INSERT OR REPLACE INTO spend_control(key, value) VALUES ('frozen', '1')")

    def unfreeze(self) -> None:
        with self._lock:
            self._c.execute("INSERT OR REPLACE INTO spend_control(key, value) VALUES ('frozen', '0')")

    # -------- sums --------
    def _sums(self, policy: SpendPolicy, now: float) -> dict:
        since = now - policy.window_seconds
        q = lambda sql, *a: self._c.execute(sql, a).fetchone()[0] or 0   # noqa: E731
        return {
            "frozen": self._frozen(),
            "total_cents": int(q("SELECT SUM(cents) FROM spend WHERE kind='pay' AND state!='void'")),
            "window": {
                "seconds": policy.window_seconds,
                "cents": int(q("SELECT SUM(cents) FROM spend WHERE kind='pay' AND state!='void' AND ts>?", since)),
                "calls": int(q("SELECT COUNT(*) FROM spend WHERE kind='pay' AND state!='void' AND ts>?", since)),
                "topup_cents": int(q("SELECT SUM(cents) FROM spend WHERE kind='topup' AND state!='void' AND ts>?", since)),
                # a paid call that was not served: an error status, or 0 = no HTTP answer at all after the box was built
                "failures": int(q("SELECT COUNT(*) FROM spend WHERE kind='pay' AND state='sent' AND http IS NOT NULL "
                                  "AND (http<200 OR http>=400) AND ts>?", since)),
            },
        }

    @staticmethod
    def _remaining(policy: SpendPolicy, s: dict) -> dict:
        w = s["window"]
        left = lambda mx, used: None if mx is None else max(0, mx - used)   # noqa: E731
        return {"total_cents": left(policy.max_total_cents, s["total_cents"]),
                "window_cents": left(policy.max_window_cents, w["cents"]),
                "window_calls": left(policy.max_window_calls, w["calls"]),
                "window_topup_cents": left(policy.max_window_topup_cents, w["topup_cents"]),
                "window_failures": left(policy.max_window_failures, w["failures"])}

    def _status(self, policy: SpendPolicy, now: float) -> dict:
        s = self._sums(policy, now)
        s["remaining"] = self._remaining(policy, s)
        s["policy"] = policy.to_dict()
        return s

    def status(self, policy: SpendPolicy, now: Optional[float] = None) -> dict:
        """frozen, lifetime total, the window's sums, what is left on each bounded axis, and the policy."""
        with self._lock:
            return self._status(policy, time.time() if now is None else now)

    # -------- reserve → settle | void --------
    def reserve(self, policy: SpendPolicy, *, kind: str, cents: int, provider: str = "", capability: str = "",
                kas_sompi: int = 0, now: Optional[float] = None) -> int:
        """Check every bound and record the spend as `reserved`, atomically. Raises SpendRefused (no row) when
        a bound would be crossed. `kind` is "pay" (kUSD to a provider) or "topup" (kUSD bought through the PSM)."""
        if kind not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}")
        cents = int(cents)
        if cents < 0:
            raise ValueError("cents must be non-negative")
        now = time.time() if now is None else float(now)
        with self._lock:
            self._c.execute("BEGIN IMMEDIATE")
            try:
                s = self._status(policy, now)
                w = s["window"]
                if s["frozen"]:
                    raise SpendRefused("frozen", "the operator froze this agent's spending", s)
                if policy.max_window_failures is not None and w["failures"] >= policy.max_window_failures:
                    raise SpendRefused("failures", f"{w['failures']} paid call(s) in the last {policy.window_seconds}s were not served "
                                       f"(limit {policy.max_window_failures}); not paying anyone until the window clears", s)
                if kind == "pay":
                    if policy.max_total_cents is not None and s["total_cents"] + cents > policy.max_total_cents:
                        raise SpendRefused("total", f"{cents}¢ would take the lifetime total to {s['total_cents'] + cents}¢, "
                                           f"over the budget of {policy.max_total_cents}¢", s)
                    if policy.max_window_cents is not None and w["cents"] + cents > policy.max_window_cents:
                        raise SpendRefused("window_cents", f"{cents}¢ would take the last {policy.window_seconds}s to {w['cents'] + cents}¢, "
                                           f"over the limit of {policy.max_window_cents}¢", s)
                    if policy.max_window_calls is not None and w["calls"] + 1 > policy.max_window_calls:
                        raise SpendRefused("window_calls", f"{w['calls']} paid call(s) in the last {policy.window_seconds}s already "
                                           f"(limit {policy.max_window_calls})", s)
                else:
                    if policy.max_window_topup_cents is not None and w["topup_cents"] + cents > policy.max_window_topup_cents:
                        raise SpendRefused("topup", f"buying {cents}¢ of kUSD would take the last {policy.window_seconds}s to "
                                           f"{w['topup_cents'] + cents}¢ of top-ups, over the limit of {policy.max_window_topup_cents}¢", s)
                cur = self._c.execute("INSERT INTO spend(ts, kind, provider, capability, cents, kas_sompi, state) VALUES (?,?,?,?,?,?,'reserved')",
                                      (now, kind, provider or "", capability or "", cents, int(kas_sompi)))
                self._c.execute("COMMIT")
                return int(cur.lastrowid)
            except BaseException:
                self._c.execute("ROLLBACK")
                raise

    def settle(self, spend_id: int, *, txid: str = "", http: Optional[int] = None, kas_sompi: Optional[int] = None) -> None:
        """The money moved: record the txid, later the HTTP status the provider answered with, and the KAS
        actually spent once known. May be called more than once for the same row."""
        with self._lock:
            self._c.execute("UPDATE spend SET state='sent', txid=CASE WHEN ?='' THEN txid ELSE ? END, "
                            "http=COALESCE(?, http), kas_sompi=COALESCE(?, kas_sompi) WHERE id=?",
                            (txid, txid, http, kas_sompi, int(spend_id)))

    def void(self, spend_id: int) -> None:
        """The reservation never became a payment (refused before signing): it no longer counts."""
        with self._lock:
            self._c.execute("UPDATE spend SET state='void' WHERE id=?", (int(spend_id),))

    def history(self, limit: int = 20) -> list:
        """Newest first."""
        with self._lock:
            rows = self._c.execute("SELECT id, ts, kind, provider, capability, cents, kas_sompi, txid, http, state FROM spend "
                                   "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]


def main(argv: Optional[list] = None) -> int:
    """The operator's CLI: `k402-spend --ledger PATH [--policy PATH] status | freeze | unfreeze | history [--limit N]`
    (also `python -m k402.spendctl …`). The agent never gets a tool for any of this; the kill switch and the
    policy are the operator's alone."""
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="k402-spend", description="Operator controls for a kUSD-market agent's spending.")
    ap.add_argument("--ledger", required=True, help="the agent's SpendLedger (sqlite file)")
    ap.add_argument("--policy", help="the agent's SpendPolicy (JSON file); status reports what it has left")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="frozen, lifetime total, window sums, remaining per bound")
    sub.add_parser("freeze", help="refuse every payment until unfreeze")
    sub.add_parser("unfreeze", help="lift a freeze")
    h = sub.add_parser("history", help="recent spends, newest first")
    h.add_argument("--limit", type=int, default=20)
    a = ap.parse_args(argv)
    try:
        policy = SpendPolicy.load(a.policy) if a.policy else SpendPolicy()
    except (OSError, ValueError) as e:
        print(f"cannot read the policy {a.policy}: {e}", file=sys.stderr)
        return 2
    try:
        led = SpendLedger(a.ledger)
    except (OSError, sqlite3.Error) as e:
        print(f"cannot open the ledger {a.ledger}: {e}", file=sys.stderr)
        return 2
    try:
        if a.cmd == "freeze":
            led.freeze()
        elif a.cmd == "unfreeze":
            led.unfreeze()
        if a.cmd in ("status", "freeze", "unfreeze"):
            st = led.status(policy)
            if not a.policy:   # without the policy, "remaining"/"policy" would read as "no bounds": print the sums only
                del st["remaining"], st["policy"]
            print(json.dumps(st, indent=1))
        elif a.cmd == "history":
            print(json.dumps(led.history(a.limit), indent=1))
    finally:
        led.close()
    return 0


__all__ = ["SpendPolicy", "SpendLedger", "SpendRefused", "REFUSAL_CODES", "main"]
