import pytest

from k402.schemes import (FacilitatorFee, ProtocolError, SessionOffer,
                          UtxoOffer, format_payment_header, parse_offers,
                          parse_payment_header, payment_required_body)


def make_offer(**overrides):
    kw = dict(network="mainnet", amount_sompi="1500000", pay_to="kaspa:qr_test",
              payment_id="p_abc123", expires=1784074500)
    kw.update(overrides)
    return UtxoOffer(**kw)


def test_offer_roundtrip():
    offer = make_offer(description="test call",
                       facilitator_fee=FacilitatorFee(sompi="2000", to="kaspa:qq_f", by="k402.dev"))
    body = payment_required_body([offer, SessionOffer(open="/onboard")])
    assert body["k402"] == "0.1"
    parsed = parse_offers(body)
    assert len(parsed) == 2
    utxo, session = parsed
    assert utxo == offer
    assert session.open == "/onboard"


def test_total_includes_facilitator_fee():
    assert make_offer().total_sompi == 1_500_000
    assert make_offer(facilitator_fee=FacilitatorFee(sompi="2000", to="x")).total_sompi == 1_502_000


def test_unknown_schemes_skipped():
    body = {"k402": "0.1", "accepts": [
        {"scheme": "kaspa-channel", "whatever": 1},
        SessionOffer(open="/o").to_dict(),
    ]}
    parsed = parse_offers(body)
    assert len(parsed) == 1 and isinstance(parsed[0], SessionOffer)


def test_non_k402_body_rejected():
    with pytest.raises(ProtocolError):
        parse_offers({"detail": "Payment Required"})


def test_float_amount_rejected():
    with pytest.raises(ProtocolError):
        UtxoOffer.from_dict(make_offer(amount_sompi="1.5").to_dict())


def test_header_roundtrip():
    hdr = format_payment_header("deadbeef", "p_abc123")
    assert parse_payment_header(hdr) == ("kaspa-utxo", "deadbeef", "p_abc123")
    with pytest.raises(ProtocolError):
        parse_payment_header("kaspa-utxo deadbeef")


def test_blockbook_offer_roundtrip():
    from k402 import BlockbookOffer, FacilitatorFee, parse_offers, payment_required_body
    offer = BlockbookOffer(coin="pearl-testnet", network="testnet", amount="500000",
                           decimals=8, pay_to="tprl1abc", payment_id="p_1", expires=1784161352,
                           description="call",
                           facilitator_fee=FacilitatorFee(sompi="1000", to="tprl1fee"))
    body = payment_required_body([offer])
    parsed = parse_offers(body)
    assert len(parsed) == 1
    o = parsed[0]
    assert o.scheme == "blockbook-utxo" and o.coin == "pearl-testnet"
    assert o == offer
    assert o.total_atomic == 501000


def test_blockbook_float_amount_rejected():
    from k402 import BlockbookOffer, ProtocolError
    import pytest as _pytest
    good = BlockbookOffer(coin="litecoin", network="mainnet", amount="500000", decimals=8,
                          pay_to="ltc1x", payment_id="p_2", expires=1).to_dict()
    good["amount"] = "0.5"
    with _pytest.raises(ProtocolError):
        BlockbookOffer.from_dict(good)
