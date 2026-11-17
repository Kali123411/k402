# The registry's machine surface, as files an agent (or a site) can fetch without running anything:
#   openapi/registry.openapi.json  — the registry's OpenAPI 3 document, exactly what the running app serves
#   schema/listing.v1.json         — JSON Schema for a signed listing (the wire format of registry.py Listing)
# `python -m k402.machine export` regenerates both; tests/test_machine_interfaces.py fails when they drift.
from __future__ import annotations

import json
import pathlib
import sys

from . import __version__

OPENAPI_PATH = "openapi/registry.openapi.json"
LISTING_SCHEMA_PATH = "schema/listing.v1.json"
SCHEMA_ID = "https://raw.githubusercontent.com/Kali123411/k402/main/schema/listing.v1.json"
HEX32 = "^[0-9a-f]{64}$"


class _NullBackend:
    """Enough of a ChainBackend to build the app for its OpenAPI document (never called)."""
    async def utxos(self, address):
        return []
    async def daa_score(self):
        return 0
    async def address_received_sompi(self, address):
        return 0


def registry_openapi() -> dict:
    """The OpenAPI 3 document of a registry built with kUSD verification on (every route present)."""
    from .registry_server import create_registry_app
    app = create_registry_app(_NullBackend(), network="testnet-10", db_path=":memory:", kusd=_NoWallet(), oracle_price=268)
    app.title = "k402 service registry"
    app.version = __version__
    app.description = ("Discovery for the Kaspa Agent Market: signed listings, reputation derived from chain-verified "
                       "settlements, and the settlements feed. The registry never touches money.")
    doc = app.openapi()
    doc.pop("servers", None)
    return doc


class _NoWallet:
    """A kUSD wallet stand-in so the kUSD routes are part of the document (never called)."""


def listing_schema() -> dict:
    """JSON Schema (draft 2020-12) for registry.py's Listing.to_dict(): what /registry/list accepts and what
    /registry/search returns per provider (plus the server-side `reputation`). v1 is the signed shape as of
    k402 0.11: KAS listings omit currency/price_cents (their signed bytes predate those fields)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "k402 listing v1",
        "description": "A provider's signed advertisement of one pay-per-call service on the Kaspa Agent Market. "
                       "Signed (BIP340) by payee_pubkey over the canonical JSON of every field but sig.",
        "type": "object",
        "additionalProperties": False,
        "required": ["capability", "endpoint", "payee_pubkey", "price_usd", "network", "schemes", "channel_terms",
                     "meta", "listed_at", "sig"],
        "properties": {
            "capability": {"type": "string", "minLength": 1, "description": "what the service does, e.g. summarize, llm:chat"},
            "endpoint": {"type": "string", "pattern": "^https?://", "description": "the 402-gated resource an agent POSTs to"},
            "payee_pubkey": {"type": "string", "pattern": HEX32, "description": "x-only key: receives payment and signs this listing"},
            "price_usd": {"type": "number", "minimum": 0, "description": "the comparable price per call in USD (for kUSD: price_cents / 100)"},
            "network": {"type": "string", "description": "mainnet | testnet-10 (kUSD runs on testnet-10 today)"},
            "schemes": {"type": "array", "minItems": 1, "items": {"type": "string"},
                        "description": "how it is paid: kusd-utxo (kUSD box), kaspa-utxo, kaspa-channel, kaspa-session, …"},
            "channel_terms": {"type": "object", "description": "min/max_sompi, maxfee_sompi, min_expiry_daa_delta for kaspa-channel"},
            "stake_outpoint": {"type": ["string", "null"], "description": "optional txid:index the provider staked"},
            "meta": {"type": "object", "description": "free-form: model, provider name, wires, region, …"},
            "description": {"type": "string", "description": "one or two lines for humans and agents"},
            "currency": {"type": "string", "enum": ["KAS", "kUSD"], "description": "pricing unit; absent means KAS"},
            "price_cents": {"type": "integer", "minimum": 0, "description": "kUSD price per call in cents (currency kUSD)"},
            "listed_at": {"type": "integer", "minimum": 0, "description": "unix seconds, stamped when signed"},
            "sig": {"type": "string", "pattern": "^[0-9a-f]{128}$", "description": "BIP340 signature by payee_pubkey"},
        },
        "allOf": [
            {"if": {"properties": {"currency": {"const": "kUSD"}}, "required": ["currency"]},
             "then": {"required": ["price_cents"], "properties": {"price_cents": {"minimum": 1}}}},
        ],
        "examples": [{
            "capability": "summarize", "endpoint": "https://summarize.kaspa-402.org/summarize",
            "payee_pubkey": "4e368c507924a26afdc91bead200b8d5c0dd9e33b44f3e144b83bcac3754a5a0", "price_usd": 0.05,
            "network": "testnet-10", "schemes": ["kusd-utxo"], "channel_terms": {}, "stake_outpoint": None,
            "meta": {"wires": ["x402", "k402"]}, "description": "Summarize text (llama3.1 8B); 5 cents of kUSD a call",
            "currency": "kUSD", "price_cents": 5, "listed_at": 1794886585, "sig": "0" * 128,
        }],
    }


def export(root: pathlib.Path) -> None:
    for rel, doc in ((OPENAPI_PATH, registry_openapi()), (LISTING_SCHEMA_PATH, listing_schema())):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n")
        print(f"wrote {rel}")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["export"]:
        print("usage: python -m k402.machine export   (writes openapi/registry.openapi.json and schema/listing.v1.json)", file=sys.stderr)
        return 2
    export(pathlib.Path(__file__).resolve().parents[1])
    return 0


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
