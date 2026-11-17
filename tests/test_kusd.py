# KusdWallet: the Python side of the kusd_pay subprocess. No chain: a fake binary answers.
import json
import os
import stat

import pytest

from k402.kusd import Family, KusdError, KusdNotRun, KusdWallet, VerifyResult

FAKE = os.path.join(os.path.dirname(__file__), "fake_kusd_pay.py")
AUTH = "06" * 32
FAKE_ENV = ("FAKE_KUSD_RESULTS", "FAKE_KUSD_LOG", "FAKE_KUSD_SLEEP")   # the child's environment is allowlisted


def mk(**kw):
    kw.setdefault("bin_path", FAKE)
    kw.setdefault("network", "testnet-10")
    kw.setdefault("authority", AUTH)
    kw.setdefault("pass_env", FAKE_ENV)
    return KusdWallet(**kw)


def keyfile(tmp_path, mode=0o600):
    p = tmp_path / "agent.key"
    p.write_text("11" * 32 + "\n")
    p.chmod(mode)
    return str(p)


def wallet(tmp_path, results, mode=0o600, monkeypatch=None):
    monkeypatch.setenv("FAKE_KUSD_RESULTS", json.dumps(results))
    monkeypatch.setenv("FAKE_KUSD_LOG", str(tmp_path / "log"))
    return mk(key_file=keyfile(tmp_path, mode), relay_url="http://relay.test")


def test_wallet_parses_bin_json(tmp_path, monkeypatch):
    w = wallet(tmp_path, {
        "family": {"authority": AUTH, "borrow_guard": "00" * 32, "ext": "ac" * 32, "network": "testnet-10"},
        "send": {"txid": "d3" * 32, "index": 0, "amount": 5, "change_cents": 35},
        "balance": {"kusd_cents": 40, "boxes": [], "kas_sompi": 100, "address": "kaspatest:q", "xonly": "ea" * 32},
        "buy": {"txid": "11" * 32, "cents": 40, "kas_in": 1492540000, "fee": 7462700},
    }, monkeypatch=monkeypatch)
    assert w.family() == Family(authority=AUTH, borrow_guard="00" * 32, ext="ac" * 32, network="testnet-10")
    assert w.send("ea" * 32, 5)["txid"] == "d3" * 32
    assert w.balance()["kusd_cents"] == 40
    assert w.buy(40)["kas_in"] == 1492540000
    argv = [json.loads(l) for l in (tmp_path / "log").read_text().splitlines()]
    assert ["send", "--to", "ea" * 32, "--cents", "5"] in argv
    # the key file travels in the environment, never on the command line
    assert not any("agent.key" in " ".join(a) for a in argv)


def test_wallet_refuses_key_file_not_600(tmp_path, monkeypatch):
    w = wallet(tmp_path, {"balance": {}}, mode=0o644, monkeypatch=monkeypatch)
    with pytest.raises(KusdNotRun, match="600"):   # KusdNotRun: the binary was never invoked (a payer may void its reservation)
        w.balance()
    assert not (tmp_path / "log").exists(), "the binary must not even be invoked"
    with pytest.raises(KusdNotRun, match="not found"):
        mk(key_file=str(tmp_path / "nope.key")).balance()
    with pytest.raises(KusdNotRun, match="could not run"):
        mk(bin_path=str(tmp_path / "no-such-binary"), key_file=keyfile(tmp_path)).family()
    assert issubclass(KusdNotRun, KusdError)


def test_verify_result_retryable_when_not_indexed(tmp_path, monkeypatch):
    w = wallet(tmp_path, {"verify": {"ok": False, "reason": "payment ab not indexed yet at the expected box address", "retryable": True}}, monkeypatch=monkeypatch)
    r = w.verify("ab" * 32, "ea" * 32, 5, AUTH, 624000000)
    assert r == VerifyResult(ok=False, reason="payment ab not indexed yet at the expected box address", index=None, daa=None, retryable=True)
    w2 = wallet(tmp_path, {"verify": {"ok": True, "index": 0, "daa": 624769575, "address": "kaspatest:p"}}, monkeypatch=monkeypatch)
    assert w2.verify("ab" * 32, "ea" * 32, 5, AUTH, 1) == VerifyResult(ok=True, reason="", index=0, daa=624769575, retryable=False)


def test_bin_failure_is_a_kusd_error(tmp_path, monkeypatch):
    w = wallet(tmp_path, {"send": {"error": "refused before signing: not enough kUSD"}, "send_exit": 1}, monkeypatch=monkeypatch)
    with pytest.raises(KusdError, match="not enough kUSD"):
        w.send("ea" * 32, 5)


def test_send_that_times_out_after_printing_still_returns_the_txid(tmp_path, monkeypatch):
    # the binary prints its result and then publishes to the relay; a caller that times out during the publish
    # must still get the txid, or it would pay twice
    w = wallet(tmp_path, {"send": {"txid": "d3" * 32, "index": 0, "amount": 5, "change_cents": 35}}, monkeypatch=monkeypatch)
    monkeypatch.setenv("FAKE_KUSD_SLEEP", "2")
    w.timeout = 0.4
    assert w.send("ea" * 32, 5)["txid"] == "d3" * 32


def test_wallet_errors_are_kusd_errors_never_raw_exceptions(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KUSD_RESULTS", json.dumps({"balance": {"kusd_cents": 1, "xonly": "ea" * 32}}))
    missing = mk(key_file=str(tmp_path / "nope.key"))
    with pytest.raises(KusdError, match="nope.key"):
        missing.balance()
    # a symlink to the key is refused (the mode check would be on the link's target of someone else's choosing)
    real = keyfile(tmp_path)
    link = tmp_path / "link.key"
    link.symlink_to(real)
    with pytest.raises(KusdError, match="symlink"):
        mk(key_file=str(link)).balance()
    # ~ is expanded
    monkeypatch.setenv("HOME", str(tmp_path))
    assert mk(key_file="~/agent.key").balance()["kusd_cents"] == 1
    # non-JSON stdout with exit 0 is a KusdError, not a KeyError later
    monkeypatch.setenv("FAKE_KUSD_RESULTS", json.dumps({"family": "not an object"}))
    with pytest.raises(KusdError, match="JSON"):
        mk(key_file=real).family()
    # the binary gets an allowlisted environment, not the caller's whole one
    monkeypatch.setenv("MY_API_SECRET", "x")
    env = mk(key_file=real)._env(True)
    assert "MY_API_SECRET" not in env and env["KUSD_KEY_FILE"] == real and "PATH" in env


def test_wallet_claim_records_a_box_paid_to_a_derived_key(tmp_path, monkeypatch):
    w = wallet(tmp_path, {"claim": {"ok": True, "cents": 5}}, monkeypatch=monkeypatch)
    assert w.claim("d3" * 32, 0, 5, "ab" * 32) == {"ok": True, "cents": 5}
    argv = [json.loads(l) for l in (tmp_path / "log").read_text().splitlines()]
    assert ["claim", "--txid", "d3" * 32, "--index", "0", "--cents", "5", "--tweak", "ab" * 32] in argv


def test_wallet_pins_the_family_and_sends_no_api_flag(tmp_path, monkeypatch):
    # the binary verifies on a node only (plain KAS at the box address has no covenant id); the family is
    # pinned by KUSD_AUTHORITY so a relay cannot substitute its own protocol boxes
    monkeypatch.setenv("KUSD_AUTHORITY", "ff" * 32)   # a stale environment value never leaks through
    w = mk(key_file=keyfile(tmp_path, 0o600), relay_url="http://relay.test")
    env = w._env(False)
    assert env["KUSD_AUTHORITY"] == "06" * 32
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FAKE_KUSD_LOG", str(log))
    monkeypatch.setenv("FAKE_KUSD_RESULTS", json.dumps({"verify": {"ok": True, "index": 0, "daa": 1}}))
    w.verify("ab" * 32, "cd" * 32, 5, "06" * 32, 0)
    assert "--api" not in log.read_text()
    assert not hasattr(w, "api_fallback")
