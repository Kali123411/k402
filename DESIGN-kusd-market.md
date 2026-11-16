# The kUSD market — k402's service exchange, priced and settled in kUSD, open only to agents

Date 2026-11-16 (revised after the builder check the same day). Builds on `DESIGN-service-exchange.md` (the open registry, direct provider
settlement, `ChannelPayer`) and on kUSD v2 (`kusd-framework`, live on TN10). The owner asked for the
design without a conversation; every choice marked *(call)* is the builder's, with its reason, and
open to the owner.

## Intent

Agents already buy services from each other over k402 and settle in KAS. KAS is volatile, so every
listing's real price moves with the market and a provider's revenue is a bet on KAS. kUSD is a
covenant-native dollar on the same chain. Pricing and settling the exchange in kUSD gives both sides
a stable unit, and gives kUSD its first native demand: agents paying agents.

"Only accessible by agents using k402" is a property of the protocol, not a login: everything in the
market is reached through k402's 402 → pay → retry flow with a kUSD payment, so the only way in is a
client that holds a key, holds kUSD, and speaks k402. There is no checkout page and no account.

## What exists and is reused

- **k402** (`~/k402`, Python, PyPI): the 402 offer/header protocol (`PROTOCOL.md`, `schemes.py`), the
  provider side (`K402.paid`, `verify`, `install`), the registry (`Listing` signed by the payee key,
  `RegistryClient`, `create_registry_app`, reputation from chain-verified settlement), the agent side
  (`ChannelPayer.discover / rank / pay / pay_best`), the MCP server (`k402-mcp`: `registry_search`,
  `registry_provider`, …). Settlement schemes today: `kaspa-utxo` (per call, fresh address) and
  `kaspa-channel` (vouchers, covenant close).
- **kUSD v2** (`kusd-framework`): kUSD is a covenant token; a holder box carries `amount` (cents) in
  state, owned by an x-only key (scheme `0x00`), moved by the `transfer` entry; `kusd-client` builds
  every user flow key-free (`send`, `swap_in`, …) and is embedded in the browser app; the state
  relay (`services/kusd-relay`) serves verified protocol and holder boxes; the public TN10 REST API
  serves UTXOs with their covenant id. Every kUSD box must hold ≥ 1 KAS of KAS (KIP-9); a new holder
  box carries 1.5 KAS (`HOLDER_VALUE`).
- A payee in kUSD is identified by the same x-only key as a k402 payee, so a provider's listing,
  channel payee and kUSD box owner are one key.

## Goals

1. A provider lists a service priced in kUSD and is paid in kUSD boxes it alone controls; the
   operator is never in the money path (the exchange's founding rule).
2. An agent discovers kUSD-priced services, pays a call with kUSD it holds, and can top up kUSD from
   the PSM with KAS on its own when short.
3. A provider verifies a kUSD payment against the chain itself, with no trust in the relay or the
   operator.
4. Reputation counts settled kUSD, chain-verified.
5. Everything runs on TN10 against the live kUSD family first; mainnet is a configuration change.

## Non-goals (this design)

- Streaming kUSD channels. Designed below as phase 2; v1 settles per call.
- A human-facing market UI. The `/kusd` page may show the market's totals later; not here.
- Agent identity or attestation. "Agent-only" is protocol-only; who holds the key is not asked.
- Escrow in kUSD (`k402.sil`-style claim-on-receipt). Later, same pattern as the KAS escrow.

## Anti-goals

- Anything that puts the operator, the registry or the relay in a position to say "paid" when the
  chain says otherwise, or to hold funds.
- A price floor hidden from providers: the KAS a kUSD box must carry is stated in the listing math.
- Breaking existing KAS listings, payers or the wire format: `kusd-utxo` is one more scheme.

## The mechanism

### 1. The wire: x402 first, k402's own header as the alias *(call, owner's direction)*

The market speaks **x402 V2**, the protocol the rest of the agent-payments world uses, through the
layer k402 already has (`k402/x402.py`, `X402Server`: `PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` /
`PAYMENT-RESPONSE`, scheme `exact`, CAIP-2 networks `kaspa:testnet-10` / `kaspa:mainnet`,
`assetTransferMethod: "submitted"`). kUSD is an x402 **asset** on the Kaspa network:

```json
{ "scheme": "exact", "network": "kaspa:testnet-10", "amount": "5",
  "asset": "<kUSD authority covenant id, hex>", "payTo": "<payee x-only hex>",
  "maxTimeoutSeconds": 120,
  "extra": { "assetSymbol": "kUSD", "decimals": 2, "assetTransferMethod": "submitted",
             "payment_id": "p_…", "binding": "kusd-holder-v1" } }
```

`asset` is the authority's covenant id, which is kUSD's on-chain identity (the way USDC's asset is
its contract address); `amount` is in cents; `payTo` is the payee's x-only key (a kUSD box owner,
not a KAS address). The payer broadcasts the kUSD `send` and proves it with the txid in the
`PaymentPayload`, exactly as the KAS `exact` binding does. An x402 client that knows the kUSD asset
can pay; one that doesn't skips the offer, as x402 requires. The binding is written up as
`scheme_exact_kusd.md` beside `scheme_exact_kaspa.md`.

For k402-native clients the same payment is also offered under k402's own header as the scheme
`kusd-utxo` (PROTOCOL.md §4 gains it beside `kaspa-utxo`); both routes feed one verifier. The
k402-native offer:

```json
{ "scheme": "kusd-utxo", "network": "testnet-10",
  "amount_cents": "5", "pay_to_pubkey": "<payee x-only hex>",
  "authority": "<kUSD authority covenant id, hex>", "payment_id": "p_…", "expires": 1794800000,
  "description": "summarize: 1 call" }
```

Its payment header is the standard three tokens: `kusd-utxo <txid> <payment_id>`.

**Paying** means a transaction that creates a kUSD holder box owned by `pay_to_pubkey` with
`amount == amount_cents`, exactly, in the kUSD family named by `authority` (its covenant id). That
is what `kusd-client`'s `send` builds (one box to the recipient, change back to the payer), so the
agent's wallet already knows how.

**Why exactly, not "at least"**: a kUSD box's state, amount included, sits behind its P2SH script
hash and is revealed only when the box is spent. A verifier can therefore only rebuild the script
for one amount and compare hashes. The offer names the amount and the payer pays it. An over- or
underpaid box is still the provider's (its key owns it) but the provider cannot recognise it as this
payment without spending it, so the call is refused; the agent side never does this, it pays the
quoted amount.

**Verifying** (provider side, against its own node, trust-free): compute the holder box's address
with `kusd-client` (`box_address` of `{amount: amount_cents, owner: pay_to_pubkey, owner_scheme
0x00, borrow_scheme 0, borrow_guard, ext}`; `borrow_guard` and `ext` are family constants read once
from the authority box), ask the node for the UTXOs at that address, and require an entry whose
outpoint's transaction is `txid`, whose `covenant_id == authority`, and whose `blockDaaScore` is at
or after the DAA score the offer was created at (no replay of an old payment against a new offer);
require `txid` unused (the store records consumed txids as well as payment ids). No node indexes
transactions by id, so verification is by address, and it works while the box is unspent, which it
is at the moment of payment: only the provider holds its key. The covenant id on the UTXO is what
makes it a kUSD box: anyone can send plain KAS to the same P2SH address, and that has no covenant id,
so a verifier must see the node's UTXO entry; the public REST index lists no covenant ids and cannot
stand in (a provider needs a node, as the KAS channel provider already does). The wallet is pinned to
its family's authority covenant id (`KUSD_AUTHORITY`): the relay is a cache, and its protocol boxes
count only if they are that authority's and name each other. One consequence of amounts being hidden: a box of the
wrong amount sits at a different address, so to the verifier it looks the same as a payment not yet
indexed; the provider retries "not indexed yet" only until its quote expires, then refuses.

**Why one box per call and not a bare KAS fee**: the alternative "pay in KAS at the kUSD rate" keeps
KAS exposure for the provider, which is the thing being removed.

**The KAS in the box, stated plainly.** Every kUSD holder box must carry ≥ 1 KAS of KAS (a new one
carries 1.5 KAS, so the recipient can move it without a coin; 1 KAS when the payer is short of
coins). So a kUSD payment also moves 1–1.5 KAS of the payer's KAS to the provider; the provider owns
that KAS too (it comes back when boxes are merged or spent), but the payer's outlay per call is
`price + 1.5 KAS + 0.1 KAS fee`. At $0.027/KAS that is about four cents of KAS per call. Consequence *(call)*: kUSD per-call pricing suits calls worth a
few cents or more; sub-cent streaming waits for kUSD channels (phase 2). Listings state `price_cents`
and the SDK shows the agent the full outlay before paying.

### 2. The kUSD wallet the Python side drives: `kusd_pay` *(call)*

`kusd-client` is Rust/WASM and key-free; k402 is Python. Rather than port the covenant builder, a
small Rust binary in kusd-framework, `kusd_pay` (harness crate, beside `tn10_deploy`), does the four
things k402 needs, each as a subcommand with JSON in/out and the secret only ever read from a file:

- `kusd_pay send --to <xonly> --cents N` → builds and submits the `send`, prints `{txid, index,
  amount, change}`; reads the payer's holder boxes and coins from the node, the protocol boxes from
  the relay (verified against the node as the driver does).
- `kusd_pay verify --txid T --to <xonly> --cents N --authority <covid> --since-daa D` → prints
  `{ok, index, daa}` or `{ok: false, reason, retryable}`: the box's address for exactly `N` cents, the
  node's UTXOs at it, the matching outpoint with the family's covenant id. Node only.
- `kusd_pay buy --cents N` → `swap_in` through the PSM (the agent tops up kUSD with KAS; pays the
  PSM's fee switch, surge included).
- `kusd_pay balance` → the wallet's kUSD boxes and KAS.

k402 wraps it the way `SubprocessChannelCovenant` wraps `channel_cycle` (`k402/kusd.py`:
`KusdWallet` with `send / verify / buy / balance`), configured by `KUSD_PAY_BIN`, `KUSD_KEY_FILE`
(mode 600), `KUSD_AUTHORITY`, `KUSD_NODE_URL`, `KUSD_RELAY_URL`, `KUSD_NETWORK`. The binary prints its
result before publishing to the relay, so a caller that stops waiting still has the txid and never pays
twice; it never settles replay (the caller's store records accepted txids). Why a binary and not FFI: the same
build serves a Rust provider later, the key never enters the Python process, and the precedent is
already in the repo.

### 3. Provider side

`K402(kusd=KusdWallet(...), kusd_price_cents=…)` adds a `kusd-utxo` offer to every 402 (beside
`kaspa-utxo` / `kaspa-channel`, so a provider can accept both units) and verifies `kusd-utxo`
headers through `KusdWallet.verify`. The payee key is the provider's existing payee key. Fee
switch and PSM state are irrelevant to the provider: it receives boxes.

### 4. Listings and discovery

`Listing` gains `currency` (`"KAS"` default, `"kUSD"`) and `price_cents` (int) *(call: a new field
rather than redefining `price_usd`, so every existing signed listing keeps its bytes and signature)*.
A kUSD listing's `schemes` includes `kusd-utxo`. `price_usd` stays the comparable figure for both
currencies (for kUSD it is `price_cents / 100`). The registry's search gains `currency=`; ranking is
unchanged. `k402-mcp`'s `registry_search` gains `currency`.

### 5. Agent side

`KusdPayer(wallet: KusdWallet, registry_url, …)`, a sibling of `ChannelPayer` sharing its
`discover / rank / preflight / pay_best` shape:

- `pay(provider, path, json_body)`: GET the 402, pick the `kusd-utxo` offer, `wallet.send(pay_to,
  cents)`, retry with the header; on 402 again, surface the reason.
- `ensure_funds(cents)`: when the wallet's kUSD is short, `wallet.buy(shortfall + headroom)` through
  the PSM (the agent needs KAS; the PSM's current fee is quoted first).
- The outlay the agent is told before paying: `price_cents` plus the box's 1.5 KAS plus the network
  fee.

`k402-mcp` gains a `pay_kusd(capability, body)` tool over `KusdPayer.pay_best`, so an MCP-driven
agent needs nothing else to take part.

### 6. Reputation and the registry's own fees

A verified `kusd-utxo` payment is reported to the registry as today (`POST /registry/settled`, free,
with the txid and the amount); the registry verifies it the same way as the provider (by address
against its node, the listing's payee key, the exact amount) and adds `settled_cents` beside
`settled_kas`. Because verification is by address, a report must arrive while the box is unspent:
providers report at payment time, as `channel_server` already does for closes; a report for a box
already spent is refused *(call)*.

**Ranking** *(call)*: a search with `currency=kUSD` ranks by `settled_cents` then price; with
`currency=KAS` by `settled_kas` then price, as today; with no currency, by settled value in USD,
kUSD at face and KAS at the kUSD oracle's liquidation price read from the kUSD relay (an ordering
input only, never a payment check). Without this a kUSD-only provider would sit at the bottom with
`settled_kas = 0` forever.

**Self-payment** is the same hole KAS reputation has today: a provider can pay itself and report.
In kUSD each such report costs a 0.1 KAS fee and locks 1 KAS of KAS in a box, so it is cheap but not
free. Not solved here; stated as a known limit, as the exchange design already states it for KAS.

Listing a kUSD service costs a listing fee paid in kUSD through k402 (the registry is a provider like
any other) *(call: 10 cents on TN10)*. KAS listings keep today's fee; delist (signed) and settled
reports stay free for everyone, so nothing about the existing KAS market changes.

### 7. Agent-only, precisely

Every listed service in the kUSD market, and listing itself, answers an unpaid request with a 402
carrying a `kusd-utxo` offer, and nothing else is served: the only way in is a k402 client holding a
key and kUSD. The registry's read API (search, provider) and its free endpoints (delist, settled
reports) are what they are today: JSON for k402 clients and MCP tools, not a page. No account, no web
checkout, no browser wallet step. The design does not try to prove the caller is software: that
would be identity, and it is a non-goal.

## Phase 2 (design only): kUSD channels

kUSD boxes may be owned by a script (`OWNER_P2SH`, scheme `0x03`: the box's `owner` is a script hash
and moving it requires a co-spent input whose scriptPubKey is that P2SH). A kUSD channel covenant
(`kchannel.ag`) can therefore own a kUSD box: open = a holder box with `owner = P2SH(channel)` beside
the channel box (payer key, payee key, expiry); vouchers = cumulative cents signed by the payer;
close = the payee spends the channel box with the latest voucher, and the kUSD `transfer` (P2SH
witness naming the channel input) emits two holder boxes, payee `total` and payer `amount − total`.
One box of KAS per channel instead of per call, so sub-cent streaming in kUSD works like the KAS
channels do today. Spike before designing it fully: confirm `transfer`'s P2SH path accepts a covenant
input as the authorizing P2SH and measure the close's mass.

## Proof on TN10

1. `kusd_pay` built; a provider on murr7800 (the existing demo service) lists `summarize` at 5¢ in
   kUSD on the TN10 registry; the listing fee (10¢) paid in kUSD through k402.
2. An agent wallet with KAS only: `KusdPayer.ensure_funds` buys kUSD from the PSM (fee switch on),
   then `pay_best("summarize")` → 402 → `kusd-utxo` payment → served; the provider's verify ran against
   the chain; the registry's `settled_cents` for the provider increases by 5.
3. A replayed header (same txid) is refused; a header for a box 1¢ short (or 1¢ over) is refused; a
   payment to the wrong authority (a box of the old family) is refused; a settled report for a box
   already spent is refused by the registry.
4. Records: `k402/PROTOCOL.md` §4 `kusd-utxo`, `PROVIDERS.md` (how to list in kUSD), kusd-framework
   `contracts/v2/README.md` (the `kusd_pay` bin), the kUSD parameter review (first native demand).

## What is left to the builder

- Exact JSON shapes of `kusd_pay`'s subcommands; retry/backoff while a payment settles into the
  node's UTXO index (reuse the `kaspa-utxo` "retry shortly" rule); store schema for consumed txids.
- The offer's DAA baseline: the provider records its node's DAA score when it creates the offer.
- How `KusdWallet.verify` obtains the family constants (`borrow_guard`, `ext`): from the relay's
  authority box, checked against the chain once per process *(the builder may instead pass them as
  configuration)*.
- Whether `K402` offers kUSD by default or only when `kusd_price_cents` is set (the latter unless the
  builder finds a reason).

## Constants (all `(call)`)

| name | value |
|---|---|
| x402 | scheme `exact`, network `kaspa:testnet-10` / `kaspa:mainnet`, `asset` = authority covid, `amount` in cents, `extra.binding` `kusd-holder-v1` |
| k402-native scheme | `kusd-utxo`, header `kusd-utxo <txid> <payment_id>` |
| offer fields | `amount_cents`, `pay_to_pubkey`, `authority`, `payment_id`, `expires`, `network` |
| listing fields | `currency` ∈ {`KAS`, `kUSD`}, `price_cents` |
| registry listing fee (TN10) | 10 cents kUSD |
| demo price (TN10) | 5 cents kUSD |
| per-payment KAS carried by the box | 1.5 KAS (`HOLDER_VALUE`), the provider's to recover |
