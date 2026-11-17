# Operator spending controls for a kUSD-market agent (DESIGN-kusd-spend-controls.md).
#
# The person who funds an agent's wallet writes a SpendPolicy; KusdPayer measures every payment against it
# through a SpendLedger (sqlite) before any money moves. The policy can only tighten what the agent asks for,
# and the agent never gets a tool that edits it: that is the boundary between a wallet and an allowance.
from __future__ import annotations

import json
import re
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
        """Read the operator's policy from a JSON file."""
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


__all__ = ["SpendPolicy", "SpendRefused", "REFUSAL_CODES"]
