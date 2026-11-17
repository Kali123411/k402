# Operator spending controls: SpendPolicy (the bounds), SpendLedger (the persistent memory they are measured
# against), SpendRefused (why a payment was not made). No chain, no wallet: pure Python + sqlite.
import json

import pytest

from k402.kusd import KusdError
from k402.spend import SpendLedger, SpendPolicy, SpendRefused

PAYEE = "ab" * 32
T0 = 1_700_000_000.0


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


def test_policy_load_reads_json_file(tmp_path, monkeypatch):
    f = tmp_path / "policy.json"
    f.write_text(json.dumps({"max_per_call_cents": 5, "allow_capabilities": ["summarize", "extract"]}))
    p = SpendPolicy.load(str(f))
    assert p.max_per_call_cents == 5 and p.allow_capabilities == frozenset({"summarize", "extract"})
    (tmp_path / "bad.json").write_text("[1, 2]")
    with pytest.raises(ValueError, match="object"):
        SpendPolicy.load(str(tmp_path / "bad.json"))
    # ~ is expanded, as KusdWallet does for its key file
    monkeypatch.setenv("HOME", str(tmp_path))
    assert SpendPolicy.load("~/policy.json").max_per_call_cents == 5
    assert SpendLedger("~/led.db").path.endswith(str(tmp_path / "led.db")) and (tmp_path / "led.db").exists()


def test_spend_refused_is_a_kusd_error_with_a_code():
    e = SpendRefused("total", "lifetime budget of 100¢ spent", {"total_cents": 100})
    assert isinstance(e, KusdError) and e.code == "total" and e.status == {"total_cents": 100}
    assert "lifetime budget" in str(e) and str(e).startswith("spend refused (total)")
    with pytest.raises(ValueError):
        SpendRefused("not-a-code", "x", {})


# ------------------------------------------------------------------ SpendLedger
def refused(ledger, policy, code, **kw):
    kw.setdefault("kind", "pay")
    kw.setdefault("cents", 5)
    before = ledger.status(policy, now=kw.get("now", T0))
    with pytest.raises(SpendRefused) as e:
        ledger.reserve(policy, **kw)
    assert e.value.code == code
    assert ledger.history() == ledger.history(), "stable"
    assert ledger.status(policy, now=kw.get("now", T0)) == before, "a refusal leaves no row behind"
    return e.value


def test_ledger_reserve_counts_toward_total_and_window():
    led, pol = SpendLedger(":memory:"), SpendPolicy(max_total_cents=100, max_window_cents=50, max_window_calls=10)
    rid = led.reserve(pol, kind="pay", cents=5, provider=PAYEE, capability="summarize", kas_sompi=160_000_000, now=T0)
    st = led.status(pol, now=T0)
    assert st["total_cents"] == 5 and st["window"]["cents"] == 5 and st["window"]["calls"] == 1
    assert st["remaining"] == {"total_cents": 95, "window_cents": 45, "window_calls": 9, "window_topup_cents": None, "window_failures": None}
    led.settle(rid, txid="d3" * 32, http=200)
    assert led.status(pol, now=T0)["total_cents"] == 5, "settling changes the state, not the amount"
    row = led.history()[0]
    assert row["txid"] == "d3" * 32 and row["http"] == 200 and row["state"] == "sent" and row["kas_sompi"] == 160_000_000


def test_ledger_refuses_each_axis_with_its_code():
    led = SpendLedger(":memory:")
    # total
    pol = SpendPolicy(max_total_cents=12)
    led.reserve(pol, kind="pay", cents=5, now=T0); led.reserve(pol, kind="pay", cents=5, now=T0)
    e = refused(led, pol, "total", cents=5, now=T0)
    assert "12" in str(e) and e.status["total_cents"] == 10
    # window cents (a fresh ledger each time keeps the axes apart)
    led = SpendLedger(":memory:"); pol = SpendPolicy(max_window_cents=9)
    led.reserve(pol, kind="pay", cents=5, now=T0)
    refused(led, pol, "window_cents", cents=5, now=T0)
    # window calls
    led = SpendLedger(":memory:"); pol = SpendPolicy(max_window_calls=1)
    led.reserve(pol, kind="pay", cents=1, now=T0)
    refused(led, pol, "window_calls", cents=1, now=T0)
    # topup: bounded separately from pays, and 0 means never
    led = SpendLedger(":memory:"); pol = SpendPolicy(max_window_topup_cents=60, max_window_cents=1)
    led.reserve(pol, kind="topup", cents=53, now=T0)          # a top-up is not a pay: the 1¢ pay window does not apply
    refused(led, pol, "topup", kind="topup", cents=8, now=T0)
    refused(SpendLedger(":memory:"), SpendPolicy(max_window_topup_cents=0), "topup", kind="topup", cents=1, now=T0)
    # failures: paid calls that were not served
    led = SpendLedger(":memory:"); pol = SpendPolicy(max_window_failures=1)
    rid = led.reserve(pol, kind="pay", cents=5, now=T0); led.settle(rid, txid="aa" * 32, http=500)
    refused(led, pol, "failures", cents=5, now=T0)
    refused(led, pol, "failures", kind="topup", cents=5, now=T0)   # nor does it keep buying kUSD
    # frozen beats everything, including an otherwise unbounded policy
    led = SpendLedger(":memory:"); led.freeze()
    assert led.frozen is True
    refused(led, SpendPolicy(), "frozen", now=T0)
    led.unfreeze()
    assert led.frozen is False and led.reserve(SpendPolicy(), kind="pay", cents=1, now=T0) >= 1


def test_ledger_void_frees_the_reservation():
    led, pol = SpendLedger(":memory:"), SpendPolicy(max_total_cents=5)
    rid = led.reserve(pol, kind="pay", cents=5, now=T0)
    refused(led, pol, "total", cents=1, now=T0)
    led.void(rid)
    assert led.status(pol, now=T0)["total_cents"] == 0
    assert led.reserve(pol, kind="pay", cents=5, now=T0) != rid
    assert [r["state"] for r in led.history()] == ["reserved", "void"]


def test_ledger_window_slides():
    led, pol = SpendLedger(":memory:"), SpendPolicy(max_window_cents=5, window_seconds=600)
    led.reserve(pol, kind="pay", cents=5, now=T0)
    refused(led, pol, "window_cents", cents=5, now=T0 + 599)
    assert led.reserve(pol, kind="pay", cents=5, now=T0 + 600) >= 1, "the first row has left the window"
    assert led.status(pol, now=T0 + 600)["window"]["cents"] == 5 and led.status(pol, now=T0 + 600)["total_cents"] == 10


def test_ledger_status_and_history(tmp_path):
    path = str(tmp_path / "spend.db")
    led, pol = SpendLedger(path), SpendPolicy(max_total_cents=100, max_window_topup_cents=100, max_window_failures=3)
    a = led.reserve(pol, kind="topup", cents=53, kas_sompi=1_961_000_000, now=T0)
    led.settle(a, txid="bb" * 32, kas_sompi=1_968_462_700)
    b = led.reserve(pol, kind="pay", cents=5, provider=PAYEE, capability="summarize", kas_sompi=160_000_000, now=T0 + 1)
    led.settle(b, txid="cc" * 32, http=500)
    st = led.status(pol, now=T0 + 2)
    assert st == {"frozen": False, "total_cents": 5, "window": {"seconds": 3600, "cents": 5, "calls": 1, "topup_cents": 53, "failures": 1},
                  "remaining": {"total_cents": 95, "window_cents": None, "window_calls": None, "window_topup_cents": 47, "window_failures": 2},
                  "policy": pol.to_dict()}
    h = led.history(limit=1)
    assert len(h) == 1 and h[0]["kind"] == "pay" and h[0]["provider"] == PAYEE and h[0]["capability"] == "summarize"
    assert led.history()[1]["kas_sompi"] == 1_968_462_700, "a settle may correct the KAS out once it is known"
    # the ledger is the memory: a new handle on the same file sees the same spend
    assert SpendLedger(path).status(pol, now=T0 + 2)["total_cents"] == 5
    led.close()


def test_ledger_concurrent_reserves_never_overspend():
    import threading
    led, pol = SpendLedger(":memory:"), SpendPolicy(max_total_cents=50)
    ok, barrier = [], threading.Barrier(8)
    def go():
        barrier.wait()
        try:
            ok.append(led.reserve(pol, kind="pay", cents=10, now=T0))
        except SpendRefused:
            pass
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(ok) == 5 and led.status(pol, now=T0)["total_cents"] == 50


# ------------------------------------------------------------------ the operator's CLI, exports, version
def test_cli_status_freeze_unfreeze_history(tmp_path, capsys):
    from k402.spend import main
    led_path, pol_path = str(tmp_path / "spend.db"), str(tmp_path / "policy.json")
    (tmp_path / "policy.json").write_text(json.dumps({"max_total_cents": 100}))
    led = SpendLedger(led_path)
    led.settle(led.reserve(SpendPolicy(), kind="pay", cents=7, provider=PAYEE, now=T0), txid="d3" * 32, http=200)
    led.close()
    assert main(["--ledger", led_path, "--policy", pol_path, "status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["frozen"] is False and st["total_cents"] == 7 and st["remaining"]["total_cents"] == 93
    assert main(["--ledger", led_path, "freeze"]) == 0
    assert json.loads(capsys.readouterr().out)["frozen"] is True, "freeze prints the new status"
    assert main(["--ledger", led_path, "--policy", pol_path, "status"]) == 0
    assert json.loads(capsys.readouterr().out)["frozen"] is True
    assert SpendLedger(led_path).frozen is True, "a frozen ledger refuses the payer in another process"
    # the module entry point runs without Python's "found in sys.modules" RuntimeWarning (k402/__init__ imports spend)
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-W", "error", "-m", "k402.spendctl", "--ledger", led_path, "status"], capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["frozen"] is True, r.stderr
    assert main(["--ledger", led_path, "unfreeze"]) == 0
    capsys.readouterr()
    assert main(["--ledger", led_path, "history", "--limit", "1"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["cents"] == 7 and rows[0]["txid"] == "d3" * 32 and rows[0]["provider"] == PAYEE


def test_cli_status_without_policy_still_works(tmp_path, capsys):
    from k402.spend import main
    assert main(["--ledger", str(tmp_path / "s.db"), "status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["total_cents"] == 0 and "policy" not in st and "remaining" not in st, "no policy given: do not print one that looks unbounded"
    assert main(["--ledger", str(tmp_path / "no-such-dir" / "s.db"), "status"]) == 2
    assert "no-such-dir" in capsys.readouterr().err
    assert main(["--ledger", str(tmp_path / "s.db"), "--policy", str(tmp_path / "missing.json"), "status"]) == 2
    assert "missing.json" in capsys.readouterr().err


def test_package_exports_and_version():
    import pathlib
    import re
    import k402
    assert k402.SpendPolicy is SpendPolicy and k402.SpendLedger is SpendLedger and k402.SpendRefused is SpendRefused
    assert {"SpendPolicy", "SpendLedger", "SpendRefused"} <= set(k402.__all__)
    pyproject = pathlib.Path(k402.__file__).resolve().parents[1] / "pyproject.toml"
    assert k402.__version__ == "0.10.0" == re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M).group(1)


def test_ledger_concurrent_reserves_never_overspend_across_connections(tmp_path):
    # the agent and the operator's CLI (or two agent processes) open the same file: BEGIN IMMEDIATE, not the
    # in-process lock, is what keeps them from both passing the same remaining budget
    import threading
    path, pol = str(tmp_path / "shared.db"), SpendPolicy(max_total_cents=50)
    handles = [SpendLedger(path) for _ in range(8)]
    ok, barrier = [], threading.Barrier(8)
    def go(led):
        barrier.wait()
        try:
            ok.append(led.reserve(pol, kind="pay", cents=10, now=T0))
        except SpendRefused:
            pass
    ts = [threading.Thread(target=go, args=(h,)) for h in handles]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(ok) == 5 and SpendLedger(path).status(pol, now=T0)["total_cents"] == 50
    [h.close() for h in handles]
