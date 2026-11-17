# The kUSD market on TN10 — proof record (2026-11-16)

Everything below ran against the live kUSD family on Kaspa testnet-10 (authority
`06b7532009b87f516475923a2b4dc6edb471b220391ca7c33c6fd88ff9f5ab32`), node `192.168.4.38:16210`, relay
`:8735`, with the demo runners in `examples/` as systemd user units on one box: registry `:8791`
(`kusd_registry.py`, 10¢ kUSD listing fee, oracle price 268), provider `:8792` (`kusd_provider.py`,
`summarize` for 5¢ of kUSD a call, llama3.1 8B behind it). Keys: three fresh wallets (registry, provider,
agent), mode 600, funded with TKAS from the deploy key (`91f6b2fd…`, `eb03087f…`, `e92b4be2…`).

## The loop

| step | who | what | on chain |
|---|---|---|---|
| top-up | provider | `KusdPayer.ensure_funds` short of the 10¢ fee → buys 60¢ through the PSM (22.472 KAS in, 0.112 KAS fee) | `2da3e3a4…` |
| list | provider | `kusd_list.py`: signed kUSD listing of `summarize` at 5¢; pays the registry's 10¢ fee **on the x402 wire** (`PAYMENT-REQUIRED` → `send` → `PAYMENT-SIGNATURE`) | fee `e098cd22…` |
| discover | agent | `registry_search currency=kUSD` → one provider, `price_cents 5`, `settled_cents 0` | — |
| pay | agent | `kusd_agent.py` → `pay_best("summarize")`: x402 wire, 5¢ box to the offer's **per-offer key**, served in one attempt | `9649948a…` |
| claim | provider | `K402` verified the box on its node and told its wallet (`kusd_pay claim`): `balance` shows the box with `offer_box: true` | — |
| report | provider | `POST /registry/settled {txid, cents, payment_id}` → registry recomputes the offer's key, verifies the unspent box → `settled_cents` 5 → 10 after the second call | — |
| spend | provider | `kusd_pay send` merges a plain 40¢ box, a plain 5¢ box and the claimed per-offer 5¢ box into one 50¢ box — three inputs, two keys (`send_from`) | `793ff07a…` |

An earlier run (before the fix pass, one key for every offer) paid `9a9e76dc…` the same way and was served;
the fix pass changed the payee to a per-offer key (see "What the review changed").

## Refusals, live

| case | result |
|---|---|
| replay: the paid txid on a fresh offer (k402 header) | 402 `txid already used` |
| the same txid upper-cased | 402 `txid already used` (canonical lowercase hex) |
| 1¢ short: a 4¢ box to the 5¢ offer's key (`c66a92c9…`) | 402 "not indexed yet … retry shortly", the **same** offer re-issued; the quote's expiry ends the retries |
| a quote paid to a key that is not the listed provider's for this offer | the payer refuses **before building a box**: `payee mismatch` |
| another family's kUSD / another network | refused by the payer before paying (unit-tested; the live wallet is pinned to one family) |
| settled report for a spent box (the provider's 60¢ PSM box) | 503 "no unspent kUSD box … report before spending it" |
| plain KAS sent to a box's address | `kusd_pay verify` → `asset mismatch` (no covenant id; kusd-framework PR #44 smoke, `c9673ca4…`) |
| a KAS payment for a kUSD listing | the gate names the currency (`test_kusd_listing_fee_is_paid_in_kusd_only_on_either_wire`); the live probe stopped one step earlier at the listing's signature check, which runs first |

## What the review changed

A fresh reviewer read the branch after Tasks 2–4 and found, among 26 items, five that mattered most:
a payment was bound to the payee and the amount but **not to its payment_id** (any N¢ box to the
provider satisfied any outstanding N¢ offer; one key for every offer); the k402 402 advertised x402
but did not read `PAYMENT-SIGNATURE` back (found live first: the registry ate a 10¢ fee, `fea1b1c1…`);
txid case games were a replay; a 5¢ payment opened a 500¢ route; and the payer failed over to a second
provider after its box was already built. All are fixed with tests (`tests/test_kusd_*.py`,
`tests/test_registry_kusd.py`, kusd-framework `tests/market.rs`, `tests/client_flows.rs`); the per-offer
key is `k402/kusd_keys.py` on the Python side and `Wallet::tweaked` / `send_from` / `kusd_pay claim` on
the Rust side, pinned to each other by a shared vector.

## Known limits (stated, not deferred)

- A provider can pay itself and report it (reputation inflation); each such report costs 0.1 KAS and
  locks 1.5 KAS in a box. Same limit as KAS reputation today.
- The amount of a box sits behind its script hash: an under- or over-paid box is the provider's but
  unrecognisable as this payment, so the agent side always pays the quoted amount, and a wrong amount
  reads as "not indexed yet" until the quote expires.
- Per-call kUSD costs 1.5 KAS of box KAS plus the fee per payment (the provider recovers the box KAS
  when it merges). Sub-cent streaming is for kUSD channels (design, phase 2).
- TN10 only. Not audited. Not for real value.

## Operator spending controls, live (2026-11-17)

The agent (`ea592a3f…`, 26¢ kUSD) ran `examples/kusd_agent.py` under policies its operator wrote
(`KUSD_SPEND_POLICY`), with its ledger at `~/.kusd-market/agent-spend.db`. k402 `feat/kusd-spend-controls`.

| case | policy | result |
|---|---|---|
| over budget | `{"max_total_cents": 1}` | `refused total: 5¢ would take the lifetime total to 5¢, over the budget of 1¢` — **before any box was built**; ledger empty |
| allowed | per call 10¢, lifetime 50¢, 5 calls/h, no PSM top-ups, 1 unserved payment/h, `summarize` only | one 5¢ box on the x402 wire, served in one attempt; status after: `total_cents 5`, `remaining.total_cents 45`, `remaining.window_calls 4` | `7e3a8db0…` |
| kill switch | operator ran `k402-spend … freeze` | `refused frozen: the operator froze this agent's spending`; `unfreeze` lifted it |
| capability | same policy, `--capability extract` | `refused capability: extract is not in the operator's allowed capabilities` — before discovery |
| operator view | `k402-spend … status` / `history --limit 3` | one row: `pay`, provider `4e368c50…`, `summarize`, 5¢, `kas_sompi 160000000`, the txid, `http 200`, `sent` |

The refusals cost nothing on chain; the one payment is the only row the ledger holds. Unit-tested but not
re-run live (the live wallet had enough kUSD): the top-up bound (`max_window_topup_cents`, `0` = never
buy), the per-window cents/calls limits, the failure stop after a `PaymentSent`, the payee allowlist, the
8-thread "never overspend" race (`test_ledger_concurrent_reserves_never_overspend`).
