# KusdWallet: the Python side of the kusd_pay subprocess. No chain: a fake binary answers.
import json
import os
import stat

import pytest

from k402.kusd import Family, KusdError, KusdWallet, VerifyResult

FAKE = os.path.join(os.path.dirname(__file__), "fake_kusd_pay.py")
AUTH = "06" * 32


def keyfile(tmp_path, mode=0o600):
    p = tmp_path / "agent.key"
    p.write_text("11" * 32 + "\n")
    p.chmod(mode)
    return str(p)


def wallet(tmp_path, results, mode=0o600, monkeypatch=None):
    monkeypatch.setenv("FAKE_KUSD_RESULTS", json.dumps(results))
    monkeypatch.setenv("FAKE_KUSD_LOG", str(tmp_path / "log"))
    return KusdWallet(bin_path=FAKE, key_file=keyfile(tmp_path, mode), relay_url="http://relay.test", network="testnet-10")


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
    with pytest.raises(KusdError, match="600"):
        w.balance()
    assert not (tmp_path / "log").exists(), "the binary must not even be invoked"


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
