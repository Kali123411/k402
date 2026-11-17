# A kUSD-priced provider on TN10: the seed services of the Kaspa Agent Market (summarize, embed, translate,
# review, extract — see kusd_capabilities.py) each for a few cents of kUSD a call (KAS still accepted), served
# only to a paid request, on both wires (the 402 carries k402's body and the x402 PAYMENT-REQUIRED header).
# Each verified kUSD payment is reported to the registry at once (the box must still be unspent), which is how
# reputation accrues; the listings themselves are made by kusd_list.py (one per capability).
#
#   uvicorn --app-dir examples kusd_provider:app --host 0.0.0.0 --port 8792
#
# Env (besides kusd_common's): K402_REGISTRY (http://127.0.0.1:8791), OLLAMA_URL + SUMMARIZE_MODEL + EMBED_MODEL
# (the models that do the work), CORS_ORIGINS (browser origins that may pay from a page, e.g. the Kaspa Lab
# market page; the x402 headers are exposed to them). PRICE_CENTS/PRICE_SOMPI are gone: prices live in the catalogue.
import os

import httpx
from fastapi import Depends, FastAPI, HTTPException

from k402 import PaymentRecord, allow_cors
from k402.cors import origins_from_env
from k402.schemes import SCHEME_KUSD

from kusd_capabilities import CAPABILITIES, listing_meta, request_for, result_from
from kusd_common import backend, k402_for, wallet

REGISTRY = os.environ.get("K402_REGISTRY", "http://127.0.0.1:8791").rstrip("/")
OLLAMA = os.environ.get("OLLAMA_URL", "http://192.168.4.38:11434").rstrip("/")

w = wallet()
k402 = k402_for(w, backend(), kusd_price_cents=CAPABILITIES["summarize"].cents, kas_price_sompi=CAPABILITIES["summarize"].kas_sompi)
app = FastAPI(title="kUSD market provider — Kaspa Agent Market seed services (TN10)")
allow_cors(app, origins_from_env(os.environ.get("CORS_ORIGINS")))
k402.install(app)
http = httpx.AsyncClient(timeout=120)


async def report(payment: PaymentRecord, capability: str) -> None:
    """Tell the registry about a kUSD payment while its box is unspent (free; failures only cost reputation)."""
    if payment.meta.get("scheme") != SCHEME_KUSD:
        return
    try:   # the registry recomputes the offer's payee key from the listed key and the payment_id
        await http.post(f"{REGISTRY}/registry/settled",
                        json={"payee_pubkey": w.payee_xonly(), "txid": payment.meta["txid"], "cents": payment.amount_sompi,
                              "payment_id": payment.payment_id, "capability": capability})
    except httpx.HTTPError:
        pass


def _paid_block(payment: PaymentRecord) -> dict:
    kusd = payment.meta.get("scheme") == SCHEME_KUSD
    return {"scheme": payment.meta.get("scheme", "kaspa-utxo"), "txid": payment.meta.get("txid"),
            "cents": payment.amount_sompi if kusd else None}


def _mount(name: str) -> None:
    cap = CAPABILITIES[name]

    async def serve(body: dict, payment: PaymentRecord = Depends(k402.paid(cap.kas_sompi, f"{name}: 1 call", cents=cap.cents))):
        try:
            path, req = request_for(name, body)
        except ValueError as e:       # a paid call with an unusable body: the payment is still reported (it settled)
            await report(payment, name)
            raise HTTPException(400, f"{e}; paid {payment.meta.get('txid')}")
        r = await http.post(f"{OLLAMA}{path}", json=req)
        r.raise_for_status()
        await report(payment, name)
        return {**result_from(name, body, r.json()), "paid": _paid_block(payment)}

    serve.__name__ = name
    app.post(f"/{name}", name=name, summary=cap.description)(serve)


for _name in CAPABILITIES:
    _mount(_name)


@app.get("/healthz")
def healthz():
    # cheap: no chain call per probe. The payee is cached by the wallet after its first successful look-up
    # (a 402 has been served); until then it is simply not reported, never a 500
    return {"ok": True, "payee": getattr(w, "_xonly", None),
            "capabilities": {n: {"price_cents": c.cents, **listing_meta(n)} for n, c in CAPABILITIES.items()}}
