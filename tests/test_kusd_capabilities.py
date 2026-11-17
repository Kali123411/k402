"""The provider's capability catalogue: five real services, each a prompt (or an embedding call) against the
operator's Ollama, priced in cents, with the input/output shape an agent needs published in the listing meta."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples"))
import kusd_capabilities as caps  # noqa: E402


def test_catalogue_has_the_five_seed_services_with_positive_cent_prices():
    assert list(caps.CAPABILITIES) == ["summarize", "embed", "translate", "review", "extract"]
    for c in caps.CAPABILITIES.values():
        assert c.cents > 0 and c.kas_sompi == c.cents * caps.SOMPI_PER_CENT
        assert c.description and c.input["text"] and c.output


def test_summarize_keeps_its_wire_shape():
    url, body = caps.request_for("summarize", {"text": "Kaspa is fast.", "max_words": 12})
    assert url.endswith("/api/generate") and body["stream"] is False and "at most 12 words" in body["prompt"]
    out = caps.result_from("summarize", {"text": "x"}, {"response": "  Fast. "})
    assert out == {"summary": "Fast."}


def test_embed_calls_the_embedding_api_and_returns_the_vector():
    url, body = caps.request_for("embed", {"text": "hello kaspa"})
    assert url.endswith("/api/embed") and body == {"model": caps.EMBED_MODEL, "input": "hello kaspa"}
    out = caps.result_from("embed", {"text": "hello kaspa"}, {"embeddings": [[0.1, 0.2, 0.3]]})
    assert out == {"embedding": [0.1, 0.2, 0.3], "dims": 3, "model": caps.EMBED_MODEL}


def test_translate_defaults_to_english_and_names_the_target():
    _, body = caps.request_for("translate", {"text": "Bonjour"})
    assert "English" in body["prompt"] and "Bonjour" in body["prompt"]
    _, body = caps.request_for("translate", {"text": "Hello", "to": "German"})
    assert "German" in body["prompt"]
    assert caps.result_from("translate", {"text": "Hello", "to": "German"}, {"response": "Hallo\n"}) == {"translation": "Hallo", "to": "German"}


def test_review_reviews_code_and_extract_parses_the_models_json():
    _, body = caps.request_for("review", {"text": "def f(): pass", "language": "python"})
    assert "python" in body["prompt"] and "def f(): pass" in body["prompt"]
    assert caps.result_from("review", {"text": "x"}, {"response": "Looks fine."}) == {"review": "Looks fine."}
    _, body = caps.request_for("extract", {"text": "Alice paid Bob 5 KAS on Monday", "fields": ["who", "amount"]})
    assert body["format"] == "json" and "who" in body["prompt"] and "amount" in body["prompt"]
    out = caps.result_from("extract", {"text": "x", "fields": ["who"]}, {"response": json.dumps({"who": "Alice"})})
    assert out == {"extracted": {"who": "Alice"}}
    # a model that does not return JSON is reported as such, never a 500 after a payment
    out = caps.result_from("extract", {"text": "x"}, {"response": "not json"})
    assert out == {"extracted": None, "raw": "not json"}


def test_inputs_are_bounded_and_text_is_required():
    with pytest.raises(ValueError):
        caps.request_for("summarize", {})
    _, body = caps.request_for("review", {"text": "x" * 100_000})
    assert len(body["prompt"]) < 60_000


def test_listing_meta_publishes_the_shape_an_agent_needs():
    m = caps.listing_meta("translate")
    assert m["input"] == caps.CAPABILITIES["translate"].input and m["output"] == "translation"
    assert m["example"]["text"] and m["wires"] == ["x402", "k402"] and m["model"]
