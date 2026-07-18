# Run a k402 provider

Sell any HTTP endpoint to AI agents, paid per call in KAS — no accounts, no API
keys, no card rails. Two levels: **basic** (per-call payments, pip-only) and
**trustless channels** (a payment channel the agent opens directly to you, settled
on Kaspa L1 with no custodian). Then **list on the exchange** so agents discover you.

---

## 1. Basic provider — per-call payments (pip-only, ~10 lines)

```bash
pip install 'k402[all]'
```

```python
from fastapi import FastAPI, Depends
from k402 import K402, XpubAddressProvider, PnnBackend, SqliteStore

k402 = K402(
    address_provider=XpubAddressProvider("kpub..."),  # watch-only — you hold no keys on the server
    backend=PnnBackend(),                              # dev/test; prod: NodeBackend("ws://your-node:17110")
    store=SqliteStore("payments.db"),
)
app = FastAPI()
k402.install(app)

@app.post("/summarize")
async def summarize(body: dict, payment=Depends(k402.paid(sompi=1_500_000))):
    return {"summary": do_work(body), "paid_by_tx": payment.meta["txid"]}
```

Unpaid calls get a protocol 402 with a fresh watch-only address; paid calls run.
Replay, expiry, and quote double-spend are handled for you. This is the whole
provider — nothing else required, and your keys never touch the server.

USD-peg your prices by computing `sompi` from a USD figure at request time against a
KAS/USD feed, so a volatile KAS never means volatile pricing.

---

## 2. Trustless provider — accept payment channels

A channel lets an agent lock KAS in a Kaspa L1 covenant and pay you per call with
off-chain vouchers. You close on-chain whenever you like, and consensus enforces the
split — you can claim **at most what the agent signed**, to your own address. No
prepaid balance you have to custody, no facilitator.

```python
from k402 import K402, ChannelManager, SubprocessChannelCovenant, PnnBackend, XpubAddressProvider

channels = ChannelManager(
    payee_privkey=MY_PAYEE_KEY,                # signs closes; can ONLY pay your own address
    backend=PnnBackend(),
    covenant=SubprocessChannelCovenant(        # assembles/broadcasts the covenant txs
        bin_path="/path/to/channel_cycle", cwd="/path/to/silverscript"),
    network="mainnet",
    max_channel=500_000_000,                   # cap channel size (covenant is unaudited)
    registry_url="https://x402-compute.68cxgfyr0.workers.dev",  # auto-report closes -> reputation
)

k402 = K402(address_provider=XpubAddressProvider("kpub..."), backend=PnnBackend(),
            channel_manager=channels)          # <- every paid() endpoint now also takes kaspa-channel
app = FastAPI()
k402.install(app)                              # also mounts /channel/open, /channel/config, /channel/{id}
```

That's it — the same `k402.paid(...)` endpoints now accept a `kaspa-channel` payment
header, and `install()` adds the channel lifecycle routes. Periodically settle:

```python
import asyncio
async def settle_loop():
    while True:
        await channels.maybe_close()   # closes channels near expiry or over the claimable threshold
        await asyncio.sleep(60)
```

**Honest note on the assembler:** `SubprocessChannelCovenant` shells out to the
`channel_cycle` binary (from the k402 covenant tooling) to build and broadcast the
covenant transactions — closing a channel needs it. A pure-python assembler that
drops this dependency is on the roadmap. The reference binary keeps your key local;
it signs, it never transmits.

**Security posture:** the payee key is close-authority only — a compromise can claim
no more than agents already signed, and only to your address. The covenant is
unaudited, so keep `max_channel` small (a few KAS). Agents refund unilaterally after
expiry, so you can't strand their funds.

---

## 3. List on the service exchange

Publish a signed listing so agents discover you. The listing is signed by your payee
key (proving you control the address that gets paid); the registry never touches money.

```python
import httpx
from k402 import Listing

listing = Listing(
    capability="summarize",                    # see k402.CAPABILITIES for the conventional slugs
    endpoint="https://your-host/summarize",
    payee_pubkey=MY_PAYEE_PUBKEY,              # x-only hex; k402.payer_pubkey_from_privkey(MY_PAYEE_KEY)
    price_usd=0.002,
    network="mainnet",
    channel_terms={"min_sompi": 100_000_000, "max_sompi": 500_000_000,
                   "maxfee_sompi": 5_000_000, "min_expiry_daa_delta": 864_000},
    meta={"model": "your-model", "region": "eu"},
).sign(MY_PAYEE_KEY)

httpx.post("https://x402-compute.68cxgfyr0.workers.dev/registry/list", json=listing.to_dict())
```

You're now in the marketplace at
[`/exchange`](https://x402-compute.68cxgfyr0.workers.dev/exchange) and via
`/registry/search`. **Reputation is chain-verified settled volume** — as agents pay
and you close channels, the registry confirms each close on-chain and your settled-KAS
figure (which ranks you in search) grows. Delist any time with a signed `DELETE`.

Run your own registry instead of using the operator's — it's just
`k402.registry_server.create_registry_app(backend, network)`; the design is federated.

---

## The agent side (for testing your listing)

```python
from k402 import ChannelPayer, SubprocessChannelOpener, NodeBackend

payer = ChannelPayer(payer_privkey=KEY, opener=SubprocessChannelOpener(bin, cwd),
                     backend=NodeBackend(...), registry_url="https://.../")
providers = await payer.discover("summarize", max_price_usd=0.005)     # ranked by reputation
r = await payer.pay(providers[0], "/summarize", {"text": "..."}, price_sompi=3_000_000)
```

`discover` returns providers ranked reputation-first; `pay` opens a channel to the
chosen provider on demand and settles per call with vouchers. Routing is client-side —
the agent picks, and settles directly with the provider.

---

See [PROTOCOL.md](PROTOCOL.md) for the wire format (`kaspa-channel` is §4) and the
repo's design/plan docs for the exchange architecture. Prototype; the channel covenant
is unaudited and channel sizes are capped — feedback and holes very welcome.
