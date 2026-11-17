# The registry's machine surface: a committed OpenAPI document that matches the running app, and a versioned
# JSON Schema for listings that every listing the library produces satisfies.
import json
import pathlib

import jsonschema
import pytest

from k402.channel import payer_pubkey_from_privkey
from k402.machine import LISTING_SCHEMA_PATH, OPENAPI_PATH, listing_schema, registry_openapi
from k402.registry import Listing

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_committed_openapi_matches_the_app():
    live = registry_openapi()
    committed = json.loads((ROOT / OPENAPI_PATH).read_text())
    assert committed == live, "regenerate with: python -m k402.machine export"
    paths = set(live["paths"])
    for p in ("/registry/list", "/registry/search", "/registry/provider/{payee_pubkey}", "/registry/activity",
              "/registry/settled", "/registry/settled/{txid}", "/healthz"):
        assert p in paths, p
    assert "delete" in live["paths"]["/registry/list"], "the signed delist is part of the contract"
    assert live["info"]["version"]


def test_listing_schema_v1_accepts_what_the_library_signs_and_rejects_garbage():
    schema = listing_schema()
    assert schema["$id"].endswith("/schema/listing.v1.json")
    assert json.loads((ROOT / LISTING_SCHEMA_PATH).read_text()) == schema
    priv = "a1" * 32
    pub = payer_pubkey_from_privkey(priv)
    kas = Listing(capability="summarize", endpoint="https://x/y", payee_pubkey=pub, price_usd=0.002, network="testnet-10").sign(priv)
    kusd = Listing(capability="summarize", endpoint="https://x/y", payee_pubkey=pub, price_usd=0.05, network="testnet-10",
                   currency="kUSD", price_cents=5, schemes=["kusd-utxo"], description="5¢ a call").sign(priv)
    for l in (kas, kusd):
        jsonschema.validate(l.to_dict(), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**kusd.to_dict(), "price_cents": -1}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**kas.to_dict(), "payee_pubkey": "zz"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({k: v for k, v in kas.to_dict().items() if k != "sig"}, schema)
