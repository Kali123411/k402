# Round-trip test for the x402 V2 compatibility layer (Kaspa binding).
# Proves the wire contract: server builds a 402 -> client pays -> server extracts + settles,
# asserting the EXACT V2 field names/shapes (x402Version==2, amount, accepted, errorReason, CAIP-2).
import base64
import json

from k402 import x402


def _is_b64_json(s):
    return json.loads(base64.b64decode(s.encode()).decode())


def test_requirements_uses_v2_field_names():
    r = x402.requirements(network=x402.NETWORK_MAINNET, amount_sompi=1_500_000,
                          pay_to="kaspa:qqexample", payment_id="pid-123")
    assert r["scheme"] == "exact"
    assert r["network"] == "kaspa:mainnet" and ":" in r["network"]      # CAIP-2
    assert r["amount"] == "1500000"                                     # V2: amount, string, atomic
    assert "maxAmountRequired" not in r                                 # V1 name must be gone
    assert r["asset"] == "KAS"
    assert r["payTo"] == "kaspa:qqexample"
    assert r["maxTimeoutSeconds"] == 120
    assert r["extra"]["payment_id"] == "pid-123"
    assert r["extra"]["assetTransferMethod"] == "submitted"


def test_payment_required_shape_and_header_roundtrip():
    res = x402.resource_info("https://api.acme.ai/v1/run", description="LLM inference",
                             service_name="acme-infer", tags=["llm", "kaspa"])
    req = x402.requirements(network=x402.NETWORK_MAINNET, amount_sompi=1_500_000,
                            pay_to="kaspa:qqexample", payment_id="pid-123")
    pr = x402.payment_required(resource=res, accepts=[req])
    assert pr["x402Version"] == 2
    assert pr["resource"]["url"] == "https://api.acme.ai/v1/run"        # resource moved to top level in V2
    assert len(pr["accepts"]) >= 1
    assert "error" not in pr                                            # pruned when None

    hdr = x402.encode_required_header(pr)
    assert _is_b64_json(hdr) == pr                                      # base64-JSON round-trips
    assert x402.decode_required_header(hdr) == pr


def test_client_payload_and_server_extract_roundtrip():
    # server side
    req = x402.requirements(network=x402.NETWORK_MAINNET, amount_sompi=1_500_000,
                            pay_to="kaspa:qqexample", payment_id="pid-123")
    pr = x402.payment_required(resource=x402.resource_info("https://api/x"), accepts=[req])
    over_wire = x402.encode_required_header(pr)

    # client side: pick the one requirement it can satisfy, pay on-chain, sign the payload with the txid
    chosen = x402.decode_required_header(over_wire)["accepts"][0]
    pp = x402.payment_payload(accepted=chosen, txid="a1db5b43add0cca3", payer="kaspa:qqpayer")
    assert pp["x402Version"] == 2
    assert "accepted" in pp and "accepts" not in pp                     # V2: singular `accepted`
    assert "scheme" not in pp and "network" not in pp                  # V2: no top-level scheme/network
    assert pp["payload"]["transaction"] == "a1db5b43add0cca3"

    sig = x402.encode_signature_header(pp)

    # server side: extract exactly what k402's verify engine needs
    got = x402.extract_payment(x402.decode_signature_header(sig))
    assert got["scheme"] == "exact"
    assert got["network"] == "kaspa:mainnet"
    assert got["amount_sompi"] == 1_500_000                             # correlates back to the offer
    assert got["pay_to"] == "kaspa:qqexample"
    assert got["payment_id"] == "pid-123"
    assert got["transfer_method"] == "submitted"
    assert got["txid"] == "a1db5b43add0cca3"
    assert got["payer"] == "kaspa:qqpayer"


def test_settle_response_success_and_failure():
    ok = x402.settle_response(success=True, network=x402.NETWORK_MAINNET,
                              txid="a1db5b43", payer="kaspa:qqpayer")
    assert ok == {"success": True, "transaction": "a1db5b43",
                  "network": "kaspa:mainnet", "payer": "kaspa:qqpayer"}
    assert "errorReason" not in ok                                     # no error on success

    bad = x402.settle_response(success=False, network=x402.NETWORK_MAINNET,
                               error_reason="insufficient_funds",
                               error_message="address received 0 of 1500000")
    assert bad["success"] is False
    assert bad["transaction"] == ""                                    # V2: "" on failure, not omitted
    assert bad["errorReason"] == "insufficient_funds"                  # V2 name (not `error`)
    assert "txHash" not in bad and "networkId" not in bad              # V1/Java names must be absent

    hdr = x402.encode_response_header(bad)
    assert _is_b64_json(hdr) == bad


def test_verify_and_supported_shapes():
    v = x402.verify_response(is_valid=True, payer="kaspa:qqpayer")
    assert v == {"isValid": True, "payer": "kaspa:qqpayer"}
    bad = x402.verify_response(is_valid=False, invalid_reason="unknown_payment_id")
    assert bad["isValid"] is False and bad["invalidReason"] == "unknown_payment_id"

    sup = x402.supported(networks=[x402.NETWORK_MAINNET, x402.NETWORK_TESTNET],
                         signers={"kaspa:*": ["kaspa:qqfacilitator"]})
    assert all(k["x402Version"] == 2 and k["scheme"] == "exact" and ":" in k["network"]
               for k in sup["kinds"])
    assert sup["signers"] == {"kaspa:*": ["kaspa:qqfacilitator"]}
    assert sup["extensions"] == []


def test_at_least_one_requirement_enforced():
    import pytest
    with pytest.raises(ValueError):
        x402.payment_required(resource=x402.resource_info("https://api/x"), accepts=[])
