# Operator spending controls for kUSD-market agents

*Companion to `DESIGN-kusd-market.md` §5 (agent side). Status: design, 2026-11-17.*

## Why

An agent in the kUSD market pays with a key it holds. Today the only bound on what it spends is the
`max_price_cents` it passes itself, and an LLM-driven agent passes whatever it was asked to. The person
who funds the wallet (the **operator**) needs to set the bounds, and the agent must not be able to
loosen them from inside a tool call. This is the difference between "an agent with a wallet" and "an
agent on an allowance".

## What the operator can set

A `SpendPolicy`, written by the operator, read by the payer. Every field is optional; `None` means
"no limit on this axis". The policy can only tighten: an agent's own `max_price_cents` is honoured
when it is lower than the policy's, never when it is higher.

| Field | Meaning |
|---|---|
| `max_per_call_cents` | Ceiling on any single quote. |
| `max_total_cents` | Lifetime kUSD budget of this ledger (sum of everything sent, incl. calls that paid but were not served). |
| `window_seconds` (default 3600) | The sliding window the `max_window_*` limits are measured over. |
| `max_window_cents` | kUSD sent per window. |
| `max_window_calls` | Paid calls per window (also bounds the KAS locked in boxes: 1.5 KAS + fee each). |
| `max_window_topup_cents` | kUSD bought through the PSM per window; `0` means the agent may never convert KAS. |
| `max_window_failures` | Paid calls per window that moved money but were not served (`PaymentSent`); beyond it, stop paying anyone. |
| `allow_payees` | Listed provider keys the agent may pay (set of x-only hex). Others are filtered out of discovery and refused on a direct `pay`. |
| `allow_capabilities` | Capabilities the agent may buy. |
| `require_listed_payee` (default `True`) | Refuse a bare endpoint with no listed key, because then the payee check (`DESIGN-kusd-market.md` §5) cannot run. |

Plus a **kill switch**: the operator freezes the ledger (`frozen`) and every payment is refused until
it is unfrozen. A freeze is a row in the ledger, not a process restart.

## Where the bounds live and how they are enforced

- `k402/spend.py`: `SpendPolicy` (frozen dataclass; `from_dict`/`to_dict`/`load(path)` for a JSON
  file), `SpendLedger` (sqlite; `":memory:"` for tests), `SpendRefused(KusdError)` with a `code`.
- The **ledger** is the memory: one row per spend (`pay` or `topup`), with timestamp, provider,
  capability, cents, KAS out, txid, HTTP status, and a `state` of `reserved` / `sent` / `void`. Window
  and total sums are computed from it, so a restart cannot reset a budget.
- **Reserve before money moves.** `ledger.reserve(policy, kind, cents, …)` runs the checks and the
  insert in one `BEGIN IMMEDIATE` transaction, so two concurrent pays cannot both pass the same
  remaining budget. After `wallet.send` the row is settled with the txid; if the send raised with
  kusd_pay's `refused before signing`, the row is voided; any other send failure keeps the row counted
  (the money may have moved; the operator would rather under-spend than over-spend).
- **Order inside `KusdPayer.pay`:** quote → existing checks (family, network, payee, cap where cap is
  `min(agent's, policy's)`) → policy checks (frozen, failures, capability, payee, listed payee) → top-up
  reserve + buy → pay reserve → send → settle. Nothing is reserved for a quote that was going to be
  refused anyway.
- **No fail-over on a refusal.** `pay_best` filters discovery by `allow_payees`, and a `SpendRefused`
  propagates immediately: the next provider would hit the same budget.
- **Status** is read-only and free: `KusdPayer.spend_status()` reports frozen, lifetime total, window
  sums, remaining on each bounded axis, and the policy itself.

## Surfaces

- Library: `KusdPayer(wallet, …, policy=None, ledger=None)`. A policy without a ledger is a
  `ValueError` (an unpersisted budget is a hole). Without a policy nothing changes.
- CLI for the operator: `k402-spend --ledger PATH [--policy PATH] status | freeze | unfreeze | history [--limit N]`
  (`python -m k402.spendctl …` without the console script).
  The agent never gets a tool that writes the policy or the freeze flag; that is the boundary.
- `k402-mcp`: `pay_kusd` honours `KUSD_SPEND_POLICY` (path to the JSON file) and `KUSD_SPEND_LEDGER`
  (sqlite path, default `~/.kusd-market/spend.db`). Without a policy file the server applies a
  built-in default *(call)*: 25¢ per call, 200¢ and 20 calls per hour, 100¢ of PSM top-up per hour,
  2 unserved payments per hour. A new tool `kusd_spend_status` shows the allowance. Refusals come back
  as `{"error": …, "code": …}`.
- `examples/kusd_agent.py` takes the same two env variables.

## Non-goals

- Enforcing the policy against a hostile *process*: the agent and the payer share one process and one
  key. The policy bounds what the k402 tools will do, not what arbitrary code with the key could do.
  That is the kUSD covenant's job later (an allowance box), not this change's.
- Per-provider budgets, time-of-day rules, approvals-in-the-loop. Easy to add on the same ledger.

## Calls made here

- Defaults in `k402-mcp` when no policy file is set (above). Chosen so a fresh install cannot lose
  more than a few dollars an hour by accident; the operator raises them by writing a policy file.
- `require_listed_payee=True` inside a policy, `False` behaviour kept when there is no policy.
- A non-`refused before signing` send error counts against the budget.
- `max_total_cents` has no reset command; the operator starts a new ledger file.
