# Conformance vectors for the kaspa-channel voucher wire format (PROTOCOL.md §4). These pin the
# exact bytes an independent implementation must produce/accept, so the format can't drift silently.
# Frozen signatures verify forever even though signing itself uses random aux.
import json
import pathlib

from k402.channel import (voucher_message, voucher_digest, verify_voucher,
                          payer_pubkey_from_privkey, format_channel_header, parse_channel_header)

VEC = json.loads((pathlib.Path(__file__).parent / "vectors" / "channel_vouchers.json").read_text())


def test_message_and_digest_are_deterministic():
    for v in VEC["vectors"]:
        assert voucher_message(v["channel_id"], v["total_sompi"]).hex() == v["message_hex"]
        assert voucher_digest(v["channel_id"], v["total_sompi"]).hex() == v["digest_hex"]
        # message layout: channel_id(32) || total(8, little-endian)
        assert v["message_hex"][:64] == v["channel_id"]
        assert int.from_bytes(bytes.fromhex(v["message_hex"][64:]), "little") == v["total_sompi"]


def test_frozen_signatures_verify():
    for v in VEC["vectors"]:
        assert payer_pubkey_from_privkey(v["payer_privkey"]) == v["payer_pubkey"]
        assert verify_voucher(v["payer_pubkey"], v["channel_id"], v["total_sompi"], v["signature_hex"])


def test_reject_cases_fail_verification():
    # a voucher signed for one (channel, total, key) must not verify under any other
    for r in VEC["reject"]:
        assert not verify_voucher(r["payer_pubkey"], r["channel_id"], r["total_sompi"], r["signature_hex"]), r["desc"]


def test_header_wire_format_roundtrips():
    for v in VEC["vectors"]:
        header = format_channel_header(v["channel_id"], v["total_sompi"], v["signature_hex"])
        assert header == v["header"]
        assert parse_channel_header(header) == (v["channel_id"], v["total_sompi"], v["signature_hex"])
