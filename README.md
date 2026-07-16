# k402

**HTTP 402 payments on Kaspa.** Charge (or pay) KAS per API call — no
accounts, no API keys, no card rails. Kaspa confirms in ~1 second, so a
non-custodial payment adds about a second to the first request and nothing
after that.

The wire protocol is [PROTOCOL.md](PROTOCOL.md) — one 402 body, one header,
implementable in any language. This package is the Python reference
implementation: client, FastAPI server middleware, and chain verification.

```
pip install 'k402[all]'     # client + server + kaspa SDK
pip install k402            # protocol types + client only (httpx)
```

## Sell: gate a FastAPI endpoint

```python
from fastapi import FastAPI, Depends
from k402 import K402, XpubAddressProvider, PnnBackend, SqliteStore

k402 = K402(
    address_provider=XpubAddressProvider("kpub..."),   # watch-only: server holds no keys
    backend=PnnBackend(),          # dev/test: community Public Node Network
                                   # prod: NodeBackend("ws://your-node:17110")
    store=SqliteStore("payments.db"),
)

app = FastAPI()
k402.install(app)

@app.post("/summarize")
async def summarize(body: dict, payment=Depends(k402.paid(sompi=1_500_000))):
    return {"summary": ..., "paid_by_tx": payment.meta["txid"]}
```

Unpaid calls get a protocol 402 with a fresh payment address; paid calls run.
Replay, expiry, and double-spend-of-the-quote are handled for you.

## Buy: a client that pays as it goes

```python
from k402 import Client, HotWallet

client = Client(payer=HotWallet(private_key_hex), max_kas_per_call=0.1)
r = await client.post("https://api.example.com/summarize", json={"text": ...})
```

The client hits the endpoint, gets the 402, pays the exact quoted sompi from
its wallet, retries with proof, and returns the real response. The
`max_kas_per_call` guard caps what it will ever pay (facilitator fees
included) without asking you.

No wallet? Services may also offer `kaspa-session` (prepaid balance):
`Client(session="s_...")`.

## Chain backends

| Backend | Use | Notes |
|---|---|---|
| `PnnBackend()` | development, testing | resolves a community [PNN](https://kaspa.aspectron.org/rpc/pnn.html) node via the Kaspa Resolver; dev/test-grade by PNN's own guidance |
| `NodeBackend("ws://host:17110")` | production | your own node (`kaspad --utxoindex`), wRPC Borsh endpoint |

## Design in one paragraph

Every payment gets a **fresh watch-only address**, so verification is just
"has this address received N sompi" — answerable by any UTXO-indexed node, no
tx parsing, no payloads, no custody anywhere. Payment ids are single-use and
marked atomically (replay protection). The protocol takes **no fee**; services
built on it (facilitators, hosted checkout) quote theirs as a transparent
`facilitator_fee` line item. Amounts are integer sompi strings end-to-end.

## Status

v0.3.0 — wire protocol stable enough to build against; API may move.
MIT license.
