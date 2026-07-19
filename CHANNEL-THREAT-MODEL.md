# kaspa-channel — threat model

Security analysis of the `kaspa-channel` scheme (PROTOCOL.md §4): covenant-enforced, unidirectional
payment channels on Kaspa L1. The scheme is **experimental until the covenant is audited**; this
document states what it defends against, what it assumes, and the residual risks that justify the
conservative `max_channel_sompi` cap.

## System model

A **payer** funds a channel **covenant** (`channel.sil`, ctor args `payer_pubkey, payee_pubkey,
expiry_daa, maxfee_sompi`). Its covenant id is the channel id. Per call the payer signs a **voucher**
— a BIP340 signature over `sha256(channel_id[32] || cumulative_total_sompi[8, LE])` — and sends it in
the `X-K402-Payment` header. The **payee** verifies vouchers off-chain (microseconds, no chain touch)
and closes on-chain when it likes. Two on-chain txs per channel (open + close). The covenant admits
exactly two spends:

- **close** — payee presents the latest voucher; consensus enforces the split in one tx: `total` to
  the payee (P2PK), remainder to the payer (P2PK). Closing early or late cannot change the split.
- **refund** — at/after `expiry_daa` the payer reclaims everything unclaimed.

**Trust anchor: Kaspa consensus.** No custodian, facilitator, or sequencer is in the money path. The
registry is discovery + reputation only and never touches funds.

## Assets

| Asset | Owner | Threat |
|---|---|---|
| Channel principal (funded KAS) | payer until claimed | theft, lock-up |
| The signed cumulative total | payer authorizes, payee claims | over-claim, replay |
| Payer / payee keys | respective party | key compromise |
| Reputation (settled volume) | payee | gaming |

## Threats, defenses, residual risk

**T1 — Malicious or compromised payee.** A payee (or someone who steals its key) tries to take more
than was authorized. *Defense:* the covenant's `close` pays the payee **at most the exact `total` the
payer signed**, and only to the payee's own address; the remainder is forced back to the payer in the
same tx. A stolen payee key therefore cannot exceed already-signed value or redirect the remainder.
*Residual:* depends on the covenant enforcing the split correctly — **the core audit item (T12)**.

**T2 — Payer default after signing.** A payer consumes a call, then tries not to pay. *Defense:* the
principal is already locked in the covenant; the payee can `close` with the latest voucher unilaterally
and consensus pays it. The payer cannot claw back signed value before expiry. *Residual:* none beyond
consensus liveness — the payee must close before `expiry_daa`; see T7.

**T3 — Voucher replay.** Reuse a voucher against a different channel or payer/payee pair. *Defense:*
the message commits to `channel_id`, so a voucher is valid for exactly one covenant; a different
channel has a different id and the signature fails. Within a channel, totals are **monotonic** and the
server tracks `already_seen`, so re-presenting an old (lower) total advances nothing. Conformance
vectors in `tests/vectors/channel_vouchers.json` pin this (wrong-channel and tampered-total both fail
verification). *Residual:* none at the voucher layer.

**T4 — Over-value voucher.** Payer signs a `total` exceeding what the channel actually holds, hoping
the payee ships service it can't be paid for. *Defense (payee side):* the per-call gate rejects unless
`cumulative_total <= channel_value - maxfee - floor` — the payee never serves against value the close
tx couldn't realize (fee + dust floor reserved). *Residual:* correct `channel_value` at open time
(T10) and correct fee/floor accounting in the gate.

**T5 — Per-call gate bugs.** The hot path must reject: unknown/closed channel; `cumulative_total -
already_seen < price` (underpayment); over-value (T4); and any voucher that isn't a valid BIP340 sig by
the payer key. A bug here serves unpaid work. *Defense:* the four checks are pure functions over
in-memory channel state + the frozen voucher format; covered by `test_channel_server.py` and the
vectors. *Residual:* implementation bugs — kept in one small, tested code path on purpose.

**T6 — Channel exhaustion / griefing.** A payer opens a minimal channel and spams calls to exhaust it,
or opens many tiny channels to burden the payee. *Defense:* `min_channel_sompi` sets a funding floor;
the gate stops serving once value is used up (the payer must open a new channel); open registration is
cheap to reject. *Residual:* a payee should rate-limit `open` and per-channel calls — an operational,
not consensus, concern.

**T7 — Expiry race (close vs refund).** Near `expiry_daa`, a payer might try to `refund` the whole
principal while a valid close is in flight. *Defense:* `refund` is only spendable **at/after** expiry;
before that only `close` is valid. The payee's rule is to close with enough DAA margin before expiry
(`min_expiry_daa_delta` guarantees a wide window). *Residual:* a payee that sleeps past expiry forfeits
unclaimed value to refund — this is correct (the payer should get its money back), but payees MUST
monitor expiry. Document/automate closing well before expiry.

**T8 — Fee / dust-floor manipulation.** Both sides commit to `maxfee_sompi` as a ctor arg. A mismatch
would let a close over-spend on fees (stealing from the remainder) or become unspendable. *Defense:*
`maxfee` is in the covenant template both sides recompile; the payee's gate reserves `maxfee + floor`
before authorizing (T4). *Residual:* the covenant must cap the close tx fee at `maxfee` — **audit item**.

**T9 — Registry poisoning / reputation gaming.** Fake listings, spoofed endpoints, or inflated
reputation. *Defense:* listings are BIP340-signed by the payee key (registry rejects unsigned/mismatched);
reputation is chain-verified settled volume (each close confirmed on-chain before it counts, deduped by
txid); the registry holds no funds, so poisoning misroutes discovery but cannot move money. *Residual:*
a provider can list a bad *endpoint*; mitigated client-side by preflight + failover (`pay_best`) and by
the exchange's liveness sweep. Reputation can be self-funded (wash settlement) — it measures real KAS
moved, not honesty; treat as a lower bound on activity, not a trust score.

**T10 — Fraudulent open registration.** Register an outpoint that isn't really a correctly-funded
channel, or someone else's. *Defense:* on `open` the server independently recompiles the covenant with
`(payer_pubkey, payee_pubkey, expiry, maxfee)` and verifies on-chain that the P2SH address matches, the
outpoint exists with a covenant id, and the value is within bounds. A forged registration fails these.
*Residual:* correctness of the recompile + on-chain check; relies on the node's UTXO view.

**T11 — Covenant template substitution.** A payer funds a look-alike covenant (e.g. one that pays a
different address on close). *Defense:* template-hash-hardening — the server derives the expected P2SH
from the canonical template and rejects any address that doesn't match; the payer likewise recompiles
the canonical template. *Residual:* both sides must use the same audited `channel.sil` — pin its hash.

**T12 — Covenant correctness (the root of trust).** Everything above reduces to: does `channel.sil`
actually enforce (a) close pays exactly `total`→payee and remainder→payer, (b) fee ≤ `maxfee`, (c)
refund only at/after expiry, (d) no third spending path? This is **unaudited**. *Residual: HIGH* — the
reason the scheme is experimental and `max_channel_sompi` is capped.

**T13 — DoS.** Voucher-verify spam or open-registration spam. *Defense:* voucher verify is
microseconds and needs a valid channel; standard rate-limiting on `open` and per-channel call rate.
*Residual:* ordinary web-service hardening; not consensus-critical.

## Residual risk & the cap

The dominant residual is **T12** (covenant correctness) with **T8/T10/T11** as its supporting checks —
all resolved only by an audit of `channel.sil` and the open-verification path. Until then, providers
SHOULD cap `max_channel_sompi` conservatively (reference: 5 KAS) so the most a covenant bug can put at
risk per channel is bounded. The cap is enforced advisorily in listings and flagged by the exchange
validator when exceeded.

## Audit checklist (for `channel.sil`)

1. `close` pays **exactly** `voucher.total` to `payee_pubkey` (P2PK) and the **remainder** to
   `payer_pubkey` (P2PK) — no other split is spendable, for any voucher the payer signed.
2. The voucher check inside the covenant is BIP340 over `sha256(channel_id || total_le8)` with
   `total` read as unsigned 8-byte LE, matching `tests/vectors/channel_vouchers.json`.
3. Close tx fee is capped at `maxfee_sompi`; the covenant is unspendable if a close over-fees.
4. `refund` is spendable **only** at/after `expiry_daa`, and pays everything to `payer_pubkey`.
5. There is **no third spending path**; `close` and `refund` are mutually exclusive before expiry.
6. The covenant id / P2SH derivation is a pure function of the ctor args (deterministic recompile),
   so the server's open-check and the payer's compile agree bit-for-bit.
7. No malleability: a third party cannot alter outputs, fee, or the claimed total of a valid close.

## What this scheme does NOT protect against

- A payee that takes payment and doesn't deliver the service (out of band; mitigated by reputation +
  small per-call amounts + client failover, not by consensus).
- Correctness/quality of the delivered service.
- Loss of a party's own private key (standard key hygiene).
- Node-level eclipse of a party's chain view affecting the on-chain checks (run your own node).
