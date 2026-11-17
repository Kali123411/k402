# A kUSD-priced provider on TN10: `summarize` for 5 cents of kUSD a call (KAS still accepted), served only to a
# paid request, on both wires (the 402 carries k402's body and the x402 PAYMENT-REQUIRED header). Each verified
# kUSD payment is reported to the registry at once (the box must still be unspent), which is how reputation
# accrues; the listing itself is made by kusd_list.py.
#
#   uvicorn --app-dir examples kusd_provider:app --host 0.0.0.0 --port 8792
#
# Env (besides kusd_common's): PRICE_CENTS (5), PRICE_SOMPI (KAS alternative, 3_000_000), K402_REGISTRY
# (http://127.0.0.1:8791), OLLAMA_URL + SUMMARIZE_MODEL (the model that does the work), CORS_ORIGINS (browser
# origins that may pay from a page, e.g. the Kaspa Lab market page; the x402 headers are exposed to them).
import os

import httpx
from fastapi import Depends, FastAPI

from k402 import PaymentRecord, allow_cors
from k402.cors import origins_from_env
from k402.schemes import SCHEME_KUSD

from kusd_common import backend, k402_for, wallet

PRICE_CENTS = int(os.environ.get("PRICE_CENTS", "5"))
PRICE_SOMPI = int(os.environ.get("PRICE_SOMPI", "3000000"))
REGISTRY = os.environ.get("K402_REGISTRY", "http://127.0.0.1:8791").rstrip("/")
OLLAMA = os.environ.get("OLLAMA_URL", "http://192.168.4.38:11434").rstrip("/")
MODEL = os.environ.get("SUMMARIZE_MODEL", "llama3.1:8b-instruct-q4_K_M")
CAPABILITY = os.environ.get("CAPABILITY", "summarize")   # named in settled reports so the market feed says what was paid for

w = wallet()
k402 = k402_for(w, backend(), kusd_price_cents=PRICE_CENTS, kas_price_sompi=PRICE_SOMPI)
app = FastAPI(title="kUSD market provider — summarize (TN10)")
allow_cors(app, origins_from_env(os.environ.get("CORS_ORIGINS")))
k402.install(app)
http = httpx.AsyncClient(timeout=120)


async def report(payment: PaymentRecord) -> None:
    """Tell the registry about a kUSD payment while its box is unspent (free; failures only cost reputation)."""
    if payment.meta.get("scheme") != SCHEME_KUSD:
        return
    try:   # the registry recomputes the offer's payee key from the listed key and the payment_id
        await http.post(f"{REGISTRY}/registry/settled",
                        json={"payee_pubkey": w.payee_xonly(), "txid": payment.meta["txid"], "cents": payment.amount_sompi,
                              "payment_id": payment.payment_id, "capability": CAPABILITY})
    except httpx.HTTPError:
        pass


@app.post("/summarize")
async def summarize(body: dict, payment: PaymentRecord = Depends(k402.paid(PRICE_SOMPI, "summarize: 1 call", cents=PRICE_CENTS))):
    text = str(body.get("text", ""))[:20000]
    words = int(body.get("max_words", 80))
    r = await http.post(f"{OLLAMA}/api/generate", json={"model": MODEL, "stream": False,
                                                        "prompt": f"Summarize in at most {words} words:\n\n{text}"})
    r.raise_for_status()
    await report(payment)
    return {"summary": r.json().get("response", "").strip(), "paid": {"scheme": payment.meta.get("scheme", "kaspa-utxo"),
            "txid": payment.meta.get("txid"), "cents": payment.amount_sompi if payment.meta.get("scheme") == SCHEME_KUSD else None}}


@app.get("/healthz")
def healthz():
    # cheap: no chain call per probe. The payee is cached by the wallet after its first successful look-up
    # (a 402 has been served); until then it is simply not reported, never a 500
    return {"ok": True, "price_cents": PRICE_CENTS, "payee": getattr(w, "_xonly", None), "model": MODEL}
