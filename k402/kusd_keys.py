# Per-offer payee keys for kUSD payments (DESIGN-kusd-market.md §1).
#
# A kUSD holder box's address is a function of (cents, owner key, family): with one owner key for every offer,
# every N-cent offer of a provider would be paid at the same address, and anyone watching it could claim another
# payer's box with their own payment_id. So each offer is paid to the provider's key tweaked by a hash of the
# payment_id — the BIP340 x-only additive tweak (owner = lift_x(P) + t·G), the same construction secp256k1's
# `XOnlyPublicKey::add_tweak` / `Keypair::add_xonly_tweak` implement, which is what kusd_pay uses to spend the
# box. Everyone can compute the payee from the provider's listed key and the payment_id (the payer checks the
# quote against the listing); only the provider can derive the secret.
from .channel import _G, _N, _lift_x, _point_add, _point_mul, _tagged_hash

OFFER_TAG = "k402/kusd-offer"


def offer_tweak(payment_id: str) -> str:
    """The 32-byte scalar (hex) an offer tweaks the provider's key by: tagged hash of the payment_id."""
    return format(int.from_bytes(_tagged_hash(OFFER_TAG, payment_id.encode()), "big") % _N, "064x")


def derive_payee(master_xonly_hex: str, payment_id: str) -> str:
    """The x-only key that owns the box paying offer `payment_id` to the provider whose listed key is `master`."""
    p = _lift_x(int(master_xonly_hex, 16))
    if p is None:
        raise ValueError("not an x-only public key")
    q = _point_add(p, _point_mul(_G, int(offer_tweak(payment_id), 16)))
    if q is None:
        raise ValueError("degenerate tweak")
    return format(q[0], "064x")


def derive_secret(master_priv_hex: str, payment_id: str) -> str:
    """The provider's secret for `derive_payee(master, payment_id)`: the master secret, negated if its public key
    has odd y (as BIP340 signing does), plus the tweak."""
    d = int(master_priv_hex, 16) % _N
    p = _point_mul(_G, d)
    if p[1] % 2:
        d = _N - d
    return format((d + int(offer_tweak(payment_id), 16)) % _N, "064x")
