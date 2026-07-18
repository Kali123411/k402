# ChannelManager — the reusable channel-settlement middleware. These are pure-logic tests with a
# fake chain backend and a fake covenant assembler: open-verification rules and the per-call voucher
# metering hot path, with no node and no covenant binary. (The live end-to-end proof runs separately
# against TN10.)
import pytest

from k402.channel import payer_pubkey_from_privkey, sign_voucher
from k402.channel_server import FLOOR, ChannelError, ChannelManager

PAYER_PRIV = "11" * 32
PAYER_PUB = payer_pubkey_from_privkey(PAYER_PRIV)
PAYEE_PRIV = "22" * 32
COVID = "ab" * 32
CHAN_VALUE = 200_000_000
MAXFEE = 5_000_000


class FakeCovenant:
    """Deterministic stand-in for the tx assembler — no chain, no binary."""
    def __init__(self):
        self.closed = None
    def address(self, payer_pubkey, payee_pubkey, expiry_daa, maxfee):
        return f"kaspatest:chan-{payer_pubkey[:6]}-{payee_pubkey[:6]}-{expiry_daa}"
    def close(self, *, channel_id, total, **kw):
        self.closed = (channel_id, total)
        return "closetxid_" + channel_id[:8]


class FakeBackend:
    """Serves one crafted UTXO at the channel address, plus a fixed DAA score."""
    def __init__(self, addr, txid, index, value, covid, daa=1000):
        self._addr, self._txid, self._index = addr, txid, index
        self._value, self._covid, self._daa = value, covid, daa
    async def daa_score(self):
        return self._daa
    async def utxos(self, address):
        if address != self._addr:
            return []
        return [{"outpoint": {"transactionId": self._txid, "index": self._index},
                 "utxoEntry": {"amount": self._value, "covenant_id": self._covid}}]


def _mgr(backend, covenant, **kw):
    return ChannelManager(PAYEE_PRIV, backend, covenant, network="testnet",
                          min_channel=100_000_000, max_channel=500_000_000, maxfee=MAXFEE,
                          min_expiry_delta=1000, **kw)


async def _open(daa=1000, value=CHAN_VALUE, expiry=5000, covid=COVID):
    cov = FakeCovenant()
    addr = cov.address(PAYER_PUB, payer_pubkey_from_privkey(PAYEE_PRIV), expiry, MAXFEE)
    be = FakeBackend(addr, "de" * 32, 0, value, covid, daa=daa)
    mgr = _mgr(be, cov)
    res = await mgr.verify_open(PAYER_PUB, expiry, "de" * 32, 0)
    return mgr, cov, res


@pytest.mark.asyncio
async def test_open_registers_and_reports_spendable():
    mgr, _, res = await _open()
    assert res["channel"] == COVID
    assert res["spendable_sompi"] == CHAN_VALUE - MAXFEE - FLOOR


@pytest.mark.asyncio
async def test_open_rejects_expiry_too_soon():
    mgr, cov, _ = await _open()
    with pytest.raises(ValueError, match="expiry_daa too soon"):
        # expiry 1500 < daa 1000 + min_expiry_delta 1000
        await _open(daa=1000, expiry=1500)


@pytest.mark.asyncio
async def test_open_rejects_out_of_bounds_value():
    with pytest.raises(ValueError, match="outside"):
        await _open(value=50_000_000)  # below min_channel


@pytest.mark.asyncio
async def test_open_rejects_missing_covenant_id():
    with pytest.raises(ValueError, match="no covenant id"):
        await _open(covid="")


@pytest.mark.asyncio
async def test_charge_meters_cumulative_vouchers():
    mgr, _, _ = await _open()
    total = 0
    for _ in range(3):
        total += 3_000_000
        out = mgr.charge(COVID, 3_000_000, total, sign_voucher(PAYER_PRIV, COVID, total))
        assert out["ok"] and out["channel_total"] == total


@pytest.mark.asyncio
async def test_charge_rejects_bad_and_replayed_vouchers():
    mgr, _, _ = await _open()
    mgr.charge(COVID, 3_000_000, 3_000_000, sign_voucher(PAYER_PRIV, COVID, 3_000_000))
    # replay / non-advancing total
    with pytest.raises(ChannelError, match="advance"):
        mgr.charge(COVID, 3_000_000, 3_000_000, sign_voucher(PAYER_PRIV, COVID, 3_000_000))
    # forged signature (wrong key)
    with pytest.raises(ChannelError, match="invalid"):
        mgr.charge(COVID, 3_000_000, 6_000_000, sign_voucher("33" * 32, COVID, 6_000_000))
    # overdraw past the ceiling
    ceiling = CHAN_VALUE - MAXFEE - FLOOR
    with pytest.raises(ChannelError, match="ceiling"):
        mgr.charge(COVID, 3_000_000, ceiling + 1, sign_voucher(PAYER_PRIV, COVID, ceiling + 1))


@pytest.mark.asyncio
async def test_charge_unknown_channel():
    mgr, _, _ = await _open()
    with pytest.raises(ChannelError, match="unknown channel"):
        mgr.charge("cd" * 32, 3_000_000, 3_000_000, sign_voucher(PAYER_PRIV, "cd" * 32, 3_000_000))


@pytest.mark.asyncio
async def test_close_uses_latest_voucher_and_marks_closed():
    mgr, cov, _ = await _open()
    mgr.charge(COVID, 5_000_000, 5_000_000, sign_voucher(PAYER_PRIV, COVID, 5_000_000))
    txid = mgr.close(COVID)
    assert cov.closed == (COVID, 5_000_000)          # closed with the signed total
    assert mgr.status(COVID)["closed"] is True
    assert mgr.close(COVID) == txid                   # idempotent — already closed
