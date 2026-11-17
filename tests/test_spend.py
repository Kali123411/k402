# Operator spending controls: SpendPolicy (the bounds), SpendLedger (the persistent memory they are measured
# against), SpendRefused (why a payment was not made). No chain, no wallet: pure Python + sqlite.
import json

import pytest

from k402.kusd import KusdError
from k402.spend import SpendPolicy, SpendRefused

PAYEE = "ab" * 32


# ------------------------------------------------------------------ SpendPolicy
def test_policy_from_dict_roundtrip_and_sets():
    d = {"max_per_call_cents": 25, "max_total_cents": 1000, "window_seconds": 600, "max_window_cents": 200,
         "max_window_calls": 20, "max_window_topup_cents": 0, "max_window_failures": 2,
         "allow_payees": [PAYEE, "CD" * 32], "allow_capabilities": ["summarize"], "require_listed_payee": False}
    p = SpendPolicy.from_dict(d)
    assert p.max_per_call_cents == 25 and p.window_seconds == 600 and p.max_window_topup_cents == 0
    assert p.allow_payees == frozenset({PAYEE, "cd" * 32}), "payee keys are compared lowercase"
    assert p.allow_capabilities == frozenset({"summarize"}) and p.require_listed_payee is False
    back = p.to_dict()
    assert SpendPolicy.from_dict(back) == p
    assert sorted(back["allow_payees"]) == sorted([PAYEE, "cd" * 32])
    # every field optional; the empty policy bounds nothing but keeps the defaults
    e = SpendPolicy.from_dict({})
    assert e == SpendPolicy() and e.window_seconds == 3600 and e.require_listed_payee is True
    with pytest.raises(ValueError, match="unknown"):
        SpendPolicy.from_dict({"max_per_cal_cents": 1})


def test_policy_rejects_negative_and_zero_window():
    with pytest.raises(ValueError, match="max_total_cents"):
        SpendPolicy(max_total_cents=-1)
    with pytest.raises(ValueError, match="window_seconds"):
        SpendPolicy(window_seconds=0)
    with pytest.raises(ValueError, match="allow_payees"):
        SpendPolicy(allow_payees=frozenset({"zz" * 32}))
    with pytest.raises(ValueError, match="max_per_call_cents"):
        SpendPolicy.from_dict({"max_per_call_cents": "ten"})


def test_policy_cap_is_the_tighter_of_agent_and_policy():
    p = SpendPolicy(max_per_call_cents=25)
    assert p.cap(None) == 25 and p.cap(10) == 10 and p.cap(50) == 25
    free = SpendPolicy()
    assert free.cap(7) == 7 and free.cap(None) is None


def test_policy_load_reads_json_file(tmp_path):
    f = tmp_path / "policy.json"
    f.write_text(json.dumps({"max_per_call_cents": 5, "allow_capabilities": ["summarize", "extract"]}))
    p = SpendPolicy.load(str(f))
    assert p.max_per_call_cents == 5 and p.allow_capabilities == frozenset({"summarize", "extract"})
    (tmp_path / "bad.json").write_text("[1, 2]")
    with pytest.raises(ValueError, match="object"):
        SpendPolicy.load(str(tmp_path / "bad.json"))


def test_spend_refused_is_a_kusd_error_with_a_code():
    e = SpendRefused("total", "lifetime budget of 100¢ spent", {"total_cents": 100})
    assert isinstance(e, KusdError) and e.code == "total" and e.status == {"total_cents": 100}
    assert "lifetime budget" in str(e) and str(e).startswith("spend refused (total)")
    with pytest.raises(ValueError):
        SpendRefused("not-a-code", "x", {})
