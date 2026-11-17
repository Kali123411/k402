"""The capability catalogue of the kUSD market provider: real services, each one call against the operator's
Ollama (a prompt, or an embedding), priced in cents of kUSD. The provider mounts one paid route per entry and
the listing carries `listing_meta()` so an agent knows the input and output shape before it pays.

No network and no wallet here: `request_for` builds the Ollama request, `result_from` reads its answer, so the
catalogue is testable without a model running."""
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

SOMPI_PER_CENT = 600_000            # the KAS alternative price: 5¢ ≙ 3,000,000 sompi as before
MODEL = os.environ.get("SUMMARIZE_MODEL", "llama3.1:8b-instruct-q4_K_M")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
MAX_TEXT = 20_000                   # characters of input accepted per call


@dataclass(frozen=True)
class Capability:
    name: str
    cents: int
    description: str
    input: dict                      # field -> what it is (an agent-readable schema sketch)
    output: str                      # the result field an agent reads
    example: dict
    model: str
    prompt: Optional[Callable[[dict], str]] = None       # None = embedding call
    options: dict = field(default_factory=dict)          # extra Ollama fields (e.g. format=json)

    @property
    def kas_sompi(self) -> int:
        return self.cents * SOMPI_PER_CENT


def _text(body: dict) -> str:
    t = str(body.get("text") or "")
    if not t.strip():
        raise ValueError("text is required")
    return t[:MAX_TEXT]


def _summarize(b: dict) -> str:
    return f"Summarize in at most {int(b.get('max_words', 80))} words:\n\n{_text(b)}"


def _translate(b: dict) -> str:
    return (f"Translate the following text into {str(b.get('to') or 'English')[:40]}. "
            f"Reply with the translation only.\n\n{_text(b)}")


def _review(b: dict) -> str:
    lang = str(b.get("language") or "the code's own language")[:40]
    return (f"Review this {lang} code as a senior engineer: correctness bugs first, then security, then clarity. "
            f"Be concrete and brief; name the line or construct for each finding.\n\n```\n{_text(b)}\n```")


def _extract(b: dict) -> str:
    fields = b.get("fields") or ["who", "what", "when", "where", "amounts"]
    fields = [str(f)[:40] for f in list(fields)[:20]]
    return (f"Extract these fields from the text as a JSON object with exactly these keys: {', '.join(fields)}. "
            f"Use null for a field the text does not give.\n\n{_text(b)}")


CAPABILITIES = {
    "summarize": Capability("summarize", 5, f"Summarize text ({MODEL}); 5 cents of kUSD a call, KAS also accepted",
                            {"text": "the text to summarize (≤20k chars)", "max_words": "optional, default 80"}, "summary",
                            {"text": "Kaspa is a proof-of-work blockDAG with one-second blocks.", "max_words": 20}, MODEL, _summarize),
    "embed": Capability("embed", 2, f"Text embedding, 768 dims ({EMBED_MODEL}); 2 cents of kUSD a call",
                        {"text": "the text to embed (≤20k chars)"}, "embedding",
                        {"text": "covenant-native stablecoin"}, EMBED_MODEL, None),
    "translate": Capability("translate", 5, f"Translate text into any language ({MODEL}); 5 cents of kUSD a call",
                            {"text": "the text to translate (≤20k chars)", "to": "target language, default English"}, "translation",
                            {"text": "Les agents paient par appel.", "to": "English"}, MODEL, _translate),
    "review": Capability("review", 10, f"Code review: bugs, security, clarity ({MODEL}); 10 cents of kUSD a call",
                         {"text": "the code to review (≤20k chars)", "language": "optional"}, "review",
                         {"text": "def pay(x):\n    return x / 0", "language": "python"}, MODEL, _review),
    "extract": Capability("extract", 5, f"Structured extraction to JSON ({MODEL}); 5 cents of kUSD a call",
                          {"text": "the source text (≤20k chars)", "fields": "optional list of field names"}, "extracted",
                          {"text": "Alice paid Bob 5 KAS on Monday in Berlin.", "fields": ["who", "amount", "when", "where"]}, MODEL,
                          _extract, {"format": "json"}),
}


def request_for(name: str, body: dict) -> tuple[str, dict]:
    """(Ollama path, JSON) for one paid call; raises ValueError on a body the service cannot serve."""
    c = CAPABILITIES[name]
    if c.prompt is None:
        return "/api/embed", {"model": c.model, "input": _text(body)}
    return "/api/generate", {"model": c.model, "stream": False, "prompt": c.prompt(body), **c.options}


def result_from(name: str, body: dict, answer: dict) -> dict:
    """The result an agent reads, from Ollama's answer."""
    c = CAPABILITIES[name]
    if c.prompt is None:
        vec = (answer.get("embeddings") or [[]])[0]
        return {"embedding": vec, "dims": len(vec), "model": c.model}
    text = str(answer.get("response", "")).strip()
    if name == "extract":
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"extracted": None, "raw": text}
        return {"extracted": parsed}
    if name == "translate":
        return {"translation": text, "to": str(body.get("to") or "English")[:40]}
    return {c.output: text}


def listing_meta(name: str) -> dict:
    """What the listing publishes beside price and endpoint: the shape of a call, so agents can use it unseen."""
    c = CAPABILITIES[name]
    return {"model": c.model, "input": c.input, "output": c.output, "example": c.example, "wires": ["x402", "k402"]}
