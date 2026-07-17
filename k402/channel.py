# kaspa-channel — covenant-enforced unidirectional payment channels (PROTOCOL.md §4, k402 0.2).
#
# The payer locks KAS in a channel covenant on Kaspa L1 (channel id = the box's covenant id).
# Per call it signs an off-chain VOUCHER — a BIP340 schnorr signature over
#     sha256( covenant_id (32 bytes) || cumulative_total_sompi (8 bytes, little-endian) )
# The payee verifies off-chain and serves instantly; it can close on-chain at any time with the
# LATEST voucher, and consensus enforces the exact split: total -> payee, remainder -> payer.
# At/after the channel's expiry DAA score the payer reclaims anything unclaimed.
#
# This module is dependency-free (pure-python BIP340 over hashlib) and is the normative wire
# reference for the scheme. The covenant source lives in the k402 repo as channel.sil; both sides
# must compile it with ctor args (payer, payee, expiry_daa, max_fee) to agree on the P2SH address.
from __future__ import annotations

import hashlib
import os

SCHEME_CHANNEL = "kaspa-channel"

# ---------------------------------------------------------------- voucher message
def voucher_message(channel_id_hex: str, total_sompi: int) -> bytes:
    """covenant_id(32) || total(8, LE) — the exact bytes the channel covenant checks."""
    cid = bytes.fromhex(channel_id_hex)
    if len(cid) != 32:
        raise ValueError("channel id must be 32 bytes of hex")
    if not (0 < total_sompi < 2**63):
        raise ValueError("total_sompi out of range")
    return cid + total_sompi.to_bytes(8, "little")


def voucher_digest(channel_id_hex: str, total_sompi: int) -> bytes:
    return hashlib.sha256(voucher_message(channel_id_hex, total_sompi)).digest()


# ---------------------------------------------------------------- header wire format
def format_channel_header(channel_id_hex: str, total_sompi: int, voucher_hex: str) -> str:
    """X-K402-Payment value: 'kaspa-channel <channel_id> <total_sompi> <voucher>'."""
    return f"{SCHEME_CHANNEL} {channel_id_hex} {total_sompi} {voucher_hex}"


def parse_channel_header(value: str) -> tuple[str, int, str]:
    """-> (channel_id_hex, total_sompi, voucher_hex). Raises ValueError on malformed input."""
    parts = value.strip().split()
    if len(parts) != 4 or parts[0] != SCHEME_CHANNEL:
        raise ValueError("malformed kaspa-channel payment header "
                         "(want 'kaspa-channel <channel_id> <total_sompi> <voucher>')")
    cid, total, sig = parts[1], int(parts[2]), parts[3]
    if len(bytes.fromhex(cid)) != 32 or len(bytes.fromhex(sig)) != 64:
        raise ValueError("bad channel id or voucher length")
    return cid, total, sig


# ---------------------------------------------------------------- BIP340 schnorr (pure python)
# Reference implementation of BIP340 over secp256k1 — enough for voucher sign/verify without a
# native dependency. Verification is what merchants run per call; signing is client-side.
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def _point_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and a[1] != b[1]:
        return None
    if a == b:
        lam = (3 * a[0] * a[0] * pow(2 * a[1], _P - 2, _P)) % _P
    else:
        lam = ((b[1] - a[1]) * pow(b[0] - a[0], _P - 2, _P)) % _P
    x = (lam * lam - a[0] - b[0]) % _P
    return (x, (lam * (a[0] - x) - a[1]) % _P)


def _point_mul(p, n):
    r = None
    for i in range(256):
        if (n >> i) & 1:
            r = _point_add(r, p)
        p = _point_add(p, p)
    return r


def _lift_x(x: int):
    if x >= _P:
        return None
    y_sq = (pow(x, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if pow(y, 2, _P) != y_sq:
        return None
    return (x, y if y % 2 == 0 else _P - y)


def verify_voucher(payer_pubkey_hex: str, channel_id_hex: str, total_sompi: int,
                   voucher_hex: str) -> bool:
    """BIP340-verify the voucher against the payer's x-only pubkey. Merchant-side, per call."""
    try:
        pk = bytes.fromhex(payer_pubkey_hex)
        sig = bytes.fromhex(voucher_hex)
        if len(pk) != 32 or len(sig) != 64:
            return False
        msg = voucher_digest(channel_id_hex, total_sompi)
        P = _lift_x(int.from_bytes(pk, "big"))
        r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        if P is None or r >= _P or s >= _N:
            return False
        e = int.from_bytes(_tagged_hash("BIP0340/challenge", sig[:32] + pk + msg), "big") % _N
        R = _point_add(_point_mul(_G, s), _point_mul((P[0], _P - P[1]), e))
        return R is not None and R[1] % 2 == 0 and R[0] == r
    except (ValueError, TypeError):
        return False


def sign_voucher(payer_privkey_hex: str, channel_id_hex: str, total_sompi: int) -> str:
    """BIP340-sign a voucher (client-side). Returns 64-byte signature hex."""
    d0 = int.from_bytes(bytes.fromhex(payer_privkey_hex), "big")
    if not (1 <= d0 <= _N - 1):
        raise ValueError("bad private key")
    msg = voucher_digest(channel_id_hex, total_sompi)
    P = _point_mul(_G, d0)
    d = d0 if P[1] % 2 == 0 else _N - d0
    pk = P[0].to_bytes(32, "big")
    aux = os.urandom(32)
    t = (d ^ int.from_bytes(_tagged_hash("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    k0 = int.from_bytes(_tagged_hash("BIP0340/nonce", t + pk + msg), "big") % _N
    if k0 == 0:
        raise RuntimeError("nonce is zero")
    R = _point_mul(_G, k0)
    k = k0 if R[1] % 2 == 0 else _N - k0
    r = R[0].to_bytes(32, "big")
    e = int.from_bytes(_tagged_hash("BIP0340/challenge", r + pk + msg), "big") % _N
    sig = r + ((k + e * d) % _N).to_bytes(32, "big")
    if not verify_voucher(pk.hex(), channel_id_hex, total_sompi, sig.hex()):
        raise RuntimeError("self-verify failed")
    return sig.hex()


def payer_pubkey_from_privkey(payer_privkey_hex: str) -> str:
    """x-only pubkey hex for a private key (what goes in the covenant ctor + open registration)."""
    d = int.from_bytes(bytes.fromhex(payer_privkey_hex), "big")
    if not (1 <= d <= _N - 1):
        raise ValueError("bad private key")
    return _point_mul(_G, d)[0].to_bytes(32, "big").hex()
