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

## Ways to pay (schemes)

A 402 offer lists one or more **schemes**; a client satisfies any one it
understands. New to k402? Start with a session — it's the least moving parts.

| Scheme | What it is | When to use |
|---|---|---|
| `kaspa-session` | Prepaid balance: fund one address, spend it down over many calls | Simplest. Best default for agents. Merchant holds the float. |
| `kaspa-utxo` | One on-chain payment per call, to a fresh address | Non-custodial, no signup; pays ≥0.1 KAS/call (the mainnet output floor) |
| `blockbook-utxo` / `evm` | The same, in BTC/LTC/DOGE/BCH/DASH or ETH/USDC/… | Paying in a coin you already hold |
| `kaspa-channel` | **Covenant payment channel** — fund once, pay per call with off-chain vouchers, settle on L1 with no custodian | Many small calls, trustlessly; sub-cent granularity, zero per-call latency |

### `kaspa-channel` — trustless per-call settlement

A channel is a prepaid session made **trustless**: your KAS sits in a Kaspa L1
covenant, not the merchant's wallet. You fund it once, then pay per call by
signing a tiny **voucher** (a cumulative running total). The merchant can close
on-chain at any time and consensus enforces the split — it can claim *at most
what you signed*, to its own address, and you reclaim the rest. There's no
facilitator and no sequencer; the only trusted party is Kaspa consensus.

```python
from k402.channel import sign_voucher, format_channel_header, payer_pubkey_from_privkey

# 1. discover the merchant's channel terms from a 402 offer (scheme "kaspa-channel"):
#    payee_pubkey, min/max channel size, required expiry, maxfee.
# 2. compile + fund the channel covenant on-chain (payer = your key, payee from the offer),
#    then register the outpoint at the offer's `open` URL -> you get a channel id.
# 3. pay per call by signing a voucher for the new cumulative total and sending it as a header:
total_sompi = 5_000_000                              # running sum you authorise so far
voucher = sign_voucher(my_privkey_hex, channel_id, total_sompi)
headers = {"X-K402-Payment": format_channel_header(channel_id, total_sompi, voucher)}
# ...send `headers` with your request; the merchant verifies the voucher in microseconds and serves.
```

The voucher crypto is pure-Python (no native dependency) and is the normative
reference for the scheme — see [PROTOCOL.md §4](PROTOCOL.md). The channel
covenant (`channel.sil`) and its on-chain tooling live in the k402 repo.

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

v0.6.0 — protocol v0.2 adds the `kaspa-channel` scheme (covenant payment
channels, proven live on Kaspa mainnet). Wire protocol stable enough to build
against; the channel covenant is unaudited, so services cap channel sizes and
mark it experimental. API may still move.
MIT license.
