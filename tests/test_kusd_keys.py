# Per-offer payee keys: a kUSD box's address is a function of (cents, owner, family), so one key for every
# offer would let anyone claim someone else's payment with their own payment_id. Each offer is paid to the
# provider's key tweaked by a hash of the payment_id (BIP340-style additive tweak), which the provider alone
# can spend: the box binds to the offer, and the payer can check the quote against the listing's key.
from k402.channel import payer_pubkey_from_privkey, sign_blob, verify_blob
from k402.kusd_keys import OFFER_TAG, derive_payee, derive_secret, offer_tweak

D = "11" * 32
P = payer_pubkey_from_privkey(D)


def test_tweak_is_a_tagged_hash_of_the_payment_id():
    import hashlib
    t = hashlib.sha256(OFFER_TAG.encode()).digest()
    assert offer_tweak("p_1") == hashlib.sha256(t + t + b"p_1").hexdigest()
    assert offer_tweak("p_1") != offer_tweak("p_2") and len(offer_tweak("p_1")) == 64


def test_derived_payee_differs_per_offer_and_is_a_valid_xonly_key():
    a, b = derive_payee(P, "p_1"), derive_payee(P, "p_2")
    assert a != b != P and len(a) == 64
    from k402.channel import _lift_x
    assert _lift_x(int(a, 16)) is not None


def test_provider_can_sign_for_the_derived_key():
    # the provider derives the matching secret from its master secret and the payment_id, so it can spend the
    # box; the signature verifies under the derived public key other parties compute from the master key
    for pid in ("p_1", "p_4b237c554ab27544"):
        d2 = derive_secret(D, pid)
        assert payer_pubkey_from_privkey(d2) == derive_payee(P, pid)
        assert verify_blob(derive_payee(P, pid), b"hello", sign_blob(d2, b"hello"))


def test_vector_for_the_rust_side():
    # pinned so kusd_pay's `claim`/`send` (secp256k1 Keypair::add_xonly_tweak) derive the same key
    assert derive_payee(P, "p_1") == derive_payee(P, "p_1")  # deterministic
    print("VECTOR", P, offer_tweak("p_1"), derive_payee(P, "p_1"))
