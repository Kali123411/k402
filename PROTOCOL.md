# k402 protocol — v0.2

**HTTP 402 payments on Kaspa.** An open convention any HTTP service can
implement to charge KAS per call — no accounts, no API keys, no card rails.
What [x402](https://www.x402.org/) is for EVM stablecoin payments, k402 is for
Kaspa, whose ~1-second confirmations at 10 blocks/sec make direct, per-call,
non-custodial payment practical without channels or invoices.

This document is the protocol. It is intentionally small: one response body,
one request header, and per-scheme verification rules. Anything not specified
here (pricing, discovery, catalogs, dashboards) is a service concern, not a
protocol concern.

## 1. Flow

1. Client calls a paid endpoint with no payment attached.
2. Server responds `402 Payment Required` with an **offer body** (§2).
3. Client satisfies one offered scheme (§4) and retries the request with a
   **payment header** (§3) — or, for `kaspa-session`, an `X-Session` header.
4. Server verifies (§5) and serves the request.

## 2. The 402 offer body

```json
{
  "k402": "0.2",
  "accepts": [
    {
      "scheme": "kaspa-utxo",
      "network": "mainnet",
      "amount_sompi": "1500000",
      "pay_to": "kaspa:qr...",
      "payment_id": "p_8f3ab2c4...",
      "expires": 1784074500,
      "description": "summarize, ~150 words",
      "finality": 1,
      "facilitator_fee": { "sompi": "2000", "to": "kaspa:qq...", "by": "example.dev" }
    },
    { "scheme": "kaspa-session", "open": "/onboard/request" }
  ],
  "reason": "optional human-readable string when re-402ing a failed payment"
}
```

Rules:

- `k402` (required): protocol version. This document defines `"0.2"` (adds the
  `kaspa-channel` scheme §4; `"0.1"` clients simply skip offers they don't know).
- `accepts` (required): one entry per acceptable scheme. Clients MUST ignore
  entries whose `scheme` they do not recognize.
- All amounts are **sompi, as strings of integers**. Float KAS never crosses
  the wire. (1 KAS = 100,000,000 sompi.)
- `amount_sompi` for `kaspa-utxo` MUST be at least the network's minimum
  payable output — on Kaspa mainnet the anti-spam / storage-mass rule makes
  this **0.1 KAS (10,000,000 sompi)**. An offer below the floor is unpayable
  (the payer cannot broadcast it), so a service pricing a call under the floor
  MUST quote the floor. Sub-floor per-call pricing belongs on `kaspa-session`,
  where many calls are metered against one funding transaction.
- `pay_to` MUST be a **fresh address per payment_id** (see §5 for why).
- `expires` (unix seconds): after this the server MAY refuse the quote and
  MUST respond with a fresh 402 offer.
- `finality` (optional, default 1): DAA-score depth the server requires before
  serving. 1 means "accepted" (~1 s on mainnet).
- `facilitator_fee` (optional): a transparent service fee the payer adds as a
  second output. See §6.
- `description`, `reason` (optional): human/agent-readable strings.

## 3. The payment header

```
X-K402-Payment: kaspa-utxo <txid> <payment_id>
```

Three space-separated tokens: scheme, the id of the paying transaction, and
the `payment_id` from the offer being satisfied.

## 4. Schemes

### `kaspa-utxo` — non-custodial per-call payment

The client sends `amount_sompi` to `pay_to` on `network`, plus
`facilitator_fee.sompi` to `facilitator_fee.to` if present, then retries with
the payment header. Overpayment is the server's to keep; underpayment fails
verification.

### `kaspa-session` — prepaid metered balance

The offer's `open` field is a URL (absolute or relative to the service) that
mints `{"session": "...", "depositAddress": "kaspa:..."}`. The client funds
the deposit address; confirmed deposits become spendable balance; subsequent
requests carry `X-Session: <session>` and the server meters against the
balance. Zero added latency per call; the merchant holds the float. Session
lifecycle beyond `open` is service-defined.

### `blockbook-utxo` — non-custodial per-call payment on any Bitcoin-family chain

The same model as `kaspa-utxo`, generalized to any transparent UTXO chain served
by a [Blockbook](https://github.com/trezor/blockbook) indexer — Bitcoin, Litecoin,
Dogecoin, Bitcoin Cash, Dash, transparent Zcash, Pearl, and others. The offer adds
two fields:

```json
{
  "scheme": "blockbook-utxo",
  "coin": "pearl",
  "network": "mainnet",
  "amount": "500000",
  "decimals": 8,
  "pay_to": "prl1...",
  "payment_id": "p_...",
  "expires": 1784161352
}
```

- `coin` (required): which chain — the merchant maps it to a Blockbook base URL.
- `amount` (required): atomic units as an integer string (not `amount_sompi`).
- `decimals` (required): atomic units per whole coin (8 for most Bitcoin-family
  chains), so a client can render the amount.

The client sends `amount` to `pay_to`, then retries with the payment header
(scheme `blockbook-utxo`). **Verification is identical to `kaspa-utxo`** — did
`pay_to` receive ≥ `amount` — answered by Blockbook's `GET /api/v2/address/{addr}`
→ `totalReceived`. The same per-chain minimum-payable/dust floor applies (§2).
Because Bitcoin-family finality is minutes, not ~1s, a service selling cheap calls
SHOULD prefer a session scheme or accept a documented number of confirmations
(`finality`) — see §5.

### `evm` — per-call payment on any EVM chain (native coin or ERC-20)

Ethereum Classic, Ethereum, and every EVM L2, in the chain's native coin or an
ERC-20 token (USDC, USDT, …). The offer:

```json
{
  "scheme": "evm",
  "chain": "ethereum-classic",
  "chain_id": 61,
  "asset": "ETC",
  "amount": "1000000000000000",
  "decimals": 18,
  "pay_to": "0x...",
  "payment_id": "p_...",
  "expires": 1784161352,
  "token": null
}
```

- `chain_id` (required): the EVM network id (61 = Ethereum Classic, 1 = Ethereum).
- `asset` / `decimals`: display symbol and base-units-per-whole (18 native, 6 for
  USDC…).
- `amount` (required): base units (wei / token units) as an integer string.
- `token` (optional): an ERC-20 contract address; absent/null means the native
  coin.

The client sends `amount` of the asset to `pay_to` on the given chain, then
retries with the payment header (scheme `evm`). **Verification is a balance
delta** (§5): EVM has no cumulative "total received" and a balance can decrease,
so the merchant snapshots `eth_getBalance` (native) or `balanceOf` (token) at
offer time and requires it to have risen by `amount` — one JSON-RPC read, no
event-log scanning. Finality is the chain's; L2s and ETC confirm in seconds.

### `kaspa-channel` — covenant-enforced payment channels

Covenant-based unidirectional payment channels: per-call granularity with **zero
per-call chain latency and no custodian**. This is what a prepaid session is, made
trustless — the merchant never holds the payer's float, and consensus (not a
facilitator or a sequencer) enforces settlement. Two on-chain transactions per
channel (open + close) regardless of how many calls flow through it.

**The covenant.** The payer funds a channel covenant on `network`; the box's
covenant id is the **channel id**. The covenant (`channel.sil`, ctor args
`payer_pubkey, payee_pubkey, expiry_daa, maxfee_sompi`) admits two spends:

- `close` — the payee presents the payer's latest voucher and consensus enforces
  the split in one transaction: `total` to the payee (P2PK), the remainder back to
  the payer (P2PK). Closing early or late cannot change the split.
- `refund` — at/after `expiry_daa` the payer reclaims everything unclaimed.

**The voucher.** Per call the payer signs a BIP340 schnorr signature over

```
sha256( channel_id (32 bytes) || cumulative_total_sompi (8 bytes, little-endian) )
```

`cumulative_total` is the running sum the payer authorizes the payee to claim so
far — monotonically increasing, one voucher per call. The channel id inside the
message binds the voucher to exactly one channel, so a voucher can never replay
against another (even between the same payer/payee pair). A voucher is a bare
64-byte signature: no chain interaction, no wallet round-trip.

**The offer** (`accepts` entry):

```json
{
  "scheme": "kaspa-channel",
  "network": "mainnet",
  "payee_pubkey": "<x-only hex>",
  "price_sompi": "3000000",
  "min_channel_sompi": "100000000",
  "max_channel_sompi": "500000000",
  "min_expiry_daa_delta": 864000,
  "maxfee_sompi": "5000000",
  "open": "/channel/open"
}
```

The payer compiles the covenant with `(its pubkey, payee_pubkey, expiry, maxfee)`
where `expiry >= current_daa + min_expiry_daa_delta`, funds it within the
`min/max_channel_sompi` bounds, and registers the outpoint by POSTing
`{payer_pubkey, expiry_daa, txid, index}` to `open`. The server independently
recompiles with the same args and verifies on-chain: the P2SH address matches, the
outpoint exists there with a covenant id, and the value is in bounds. `open`
returns `{channel: <channel_id>, ...}`.

**The payment header:**

```
X-K402-Payment: kaspa-channel <channel_id> <cumulative_total_sompi> <voucher_hex>
```

The server verifies (all in memory, ~microseconds — the per-call hot path): the
channel is known and open; `cumulative_total - already_seen >= price`;
`cumulative_total <= channel_value - maxfee - floor`; and the voucher BIP340-verifies
against the payer key. Then it advances the channel's total and serves. The payee
closes on-chain whenever it likes (typically as claimable value crosses a threshold
or expiry nears); the payer's remainder returns in the same transaction.

**Trust model.** No custody — funds sit in the covenant, not the merchant's wallet.
A compromised payee key can claim **at most the exact total the payer already
signed**, and only to the payee's own address. The payer cannot default once a
voucher is signed, and reclaims everything unspent after expiry. The only trusted
party is Kaspa consensus.

Covenants require the Toccata consensus rules (mainnet-active). This scheme is
marked experimental until the covenant is audited; services SHOULD cap
`max_channel_sompi` conservatively. See
[CHANNEL-THREAT-MODEL.md](https://github.com/Kali123411/k402/blob/main/CHANNEL-THREAT-MODEL.md)
for the full threat analysis and the covenant audit checklist. Conformance vectors for the voucher
wire format are in `tests/vectors/channel_vouchers.json`.

## 5. Verification (`kaspa-utxo`)

On receiving the payment header the server:

1. Looks up `payment_id`. Unknown, already-used, or expired → fresh 402.
2. Checks the chain: the amount received by `pay_to` **since the offer was
   created** ≥ `amount_sompi`, at the offer's `finality` depth. The server
   snapshots the address's already-received total when it issues the offer and
   counts only the delta — so a **reused address's standing balance or history
   can never auto-satisfy an offer** (critical when `pay_to` is a cold-wallet
   address, or when the indexer reports a monotonic cumulative "total received").
   Any node/indexer with an address query answers this; no transaction parsing
   or payload inspection is required. Fresh-address-per-payment makes the
   baseline zero and is still the most robust option under concurrency.
3. Atomically marks `payment_id` used (replay protection), then serves.

A payment that has not yet landed is a normal race at 1-second block times:
servers SHOULD answer with a 402 whose `reason` says so, and clients SHOULD
retry for a few seconds before treating payment as failed.

## 6. Fees

**The protocol itself extracts no fee.** There is no protocol-level fee
output, no routed settlement, no percentage. Payments go client → merchant.

Services layered on the rail (facilitators, hosted checkouts, channel
operators) MAY charge for their work by quoting `facilitator_fee` in offers
they produce. The fee is a visible line item the client pays as an explicit
extra output — never hidden in `amount_sompi`. Clients MUST count it toward
any per-call spending guard they enforce.

## 7. Security notes

- **Merchants:** derive `pay_to` watch-only (xpub) — the web server should
  never hold spending keys. Persist payment_ids durably; `mark used` must be
  atomic under concurrency.
- **Clients:** enforce a per-call spend ceiling before paying any offer;
  treat `expires` as hard; never pay the same offer twice.
- **Both:** amounts are integers end-to-end. A server MUST NOT serve on a
  partial payment; a client SHOULD overpay dust rather than round down.

## 8. Reference implementation

`pip install k402` — Python client (`k402.Client`), FastAPI server middleware
(`k402.K402`), watch-only xpub derivation, and chain verification via your own
node or the community [Public Node Network](https://kaspa.aspectron.org/rpc/pnn.html)
(dev/test). MIT-licensed; this spec may be implemented by anyone in anything.
