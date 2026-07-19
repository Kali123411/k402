# k402

**HTTP 402 payments on Kaspa.** Charge (or pay) KAS per API call — no
accounts, no API keys, no card rails. Kaspa confirms in ~1 second, so a
non-custodial payment adds about a second to the first request and nothing
after that.

The wire protocol is [PROTOCOL.md](https://github.com/Kali123411/k402/blob/main/PROTOCOL.md) — one 402 body, one header,
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
Replay, expiry, and double-spend-of-the-quote are handled for you. That's a
complete provider — your keys never touch the server. Full guide, including
**trustless payment channels** and **listing on the service exchange**:
[PROVIDERS.md](https://github.com/Kali123411/k402/blob/main/PROVIDERS.md).

## Sell trustlessly: accept payment channels

Let an agent lock KAS in a Kaspa L1 covenant and pay you per call with off-chain
vouchers — you close on-chain when you like, and consensus enforces the split (you
can claim at most what the agent signed, to your own address). No prepaid balance to
custody, no facilitator.

```python
from k402 import K402, ChannelManager, SubprocessChannelCovenant, PnnBackend, XpubAddressProvider

channels = ChannelManager(payee_privkey=MY_KEY, backend=PnnBackend(),
                          covenant=SubprocessChannelCovenant(bin_path, cwd),
                          registry_url="https://x402-compute.68cxgfyr0.workers.dev")
k402 = K402(address_provider=XpubAddressProvider("kpub..."), backend=PnnBackend(),
            channel_manager=channels)          # paid() endpoints now also take kaspa-channel
k402.install(app)                              # + mounts /channel/open, /config, /{id}
```

## Get discovered: the service exchange

An open marketplace of agent-payable services, settled trustlessly over channels —
the registry never touches money. Publish a signed listing and agents find you;
reputation is **chain-verified settled volume**. Browse it at
[`/exchange`](https://x402-compute.68cxgfyr0.workers.dev/exchange).

```python
from k402 import Listing
listing = Listing(capability="summarize", endpoint="https://you/summarize",
                  payee_pubkey=MY_PUBKEY, price_usd=0.002).sign(MY_KEY)
httpx.post("https://x402-compute.68cxgfyr0.workers.dev/registry/list", json=listing.to_dict())
```

**Auto-route across providers.** On the agent side, `pay_best` discovers providers for a
capability, ranks them (`registry` | `cheapest` | `reputation`), and pays the first that serves the
call — **failing over** to the next on any error. A cheap preflight skips dead endpoints *before*
opening a channel (which is an on-chain cost), so you never burn KAS on a provider that's down.

```python
from k402 import ChannelPayer, SubprocessChannelOpener, NodeBackend

payer = ChannelPayer(payer_privkey=KEY, opener=SubprocessChannelOpener(bin, cwd),
                     backend=NodeBackend("ws://your-node:17110"),
                     registry_url="https://x402-compute.68cxgfyr0.workers.dev")

res = await payer.pay_best("summarize", json_body={"text": "..."},
                           policy="cheapest", max_price_usd=0.005, min_reputation_kas=1.0)
print(res.response.json(), "served by", res.provider["payee_pubkey"][:8])
# res.attempts lists any providers skipped/failed before this one. Raises RouteError if none serve.
```

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
reference for the scheme — see [PROTOCOL.md §4](https://github.com/Kali123411/k402/blob/main/PROTOCOL.md). The channel
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

v0.7 — protocol v0.2 (`kaspa-channel` covenant payment channels, live on Kaspa
mainnet) plus the **service exchange**: an open marketplace where providers list
signed services and agents discover and settle with them directly over channels,
reputation chain-verified, the registry never touching money. Wire protocol stable
enough to build against; the channel covenant is unaudited, so channel sizes are
capped and the scheme is experimental. API may still move. MIT license.
