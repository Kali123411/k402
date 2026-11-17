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
| `max_window_failures` | Paid calls per window that moved money but were not served (`PaymentSent`: an error status, or no HTTP answer at all); once that many have happened in the window, stop paying anyone. `0` means never pay. |
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
  insert in one `BEGIN IMMEDIATE` transaction, so two concurrent pays (threads or processes on the same
  file) cannot both pass the same remaining budget. After `wallet.send` the row is settled with the
  txid; the row is voided only when nothing can have been signed: the Python side never ran the binary
  (`KusdNotRun`: key file missing or not mode 600, binary not runnable) or kusd_pay said
  `refused before signing`. Any other send failure keeps the row counted (the money may have moved; the
  operator would rather under-spend than over-spend).
- **Order inside `KusdPayer.pay`:** frozen? → capability / payee / listed-payee gate (before the first
  request leaves, so nothing is sent to an endpoint the operator did not allow) → quote → per-call
  ceiling (`SpendRefused per_call`) → existing checks (family, network, payee, agent's cap) → **pay
  reserve** → top-up reserve + buy (only now, so no KAS is ever swapped for a payment the pay bounds
  refuse; a refused or failed top-up voids the pay row) → send → settle txid → paid request → settle
  HTTP status. Once the box exists, any failure, including a transport error with no answer (recorded
  as HTTP `0`), is a `PaymentSent` that counts as a failure, and `pay_best` never fails over past it.
- The top-up buys the shortfall plus 50¢ headroom; when the window's remaining top-up allowance is less
  than that but still covers the shortfall, it buys the remainder instead of refusing.
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
- `k402-mcp`: `pay_kusd` honours `KUSD_SPEND_POLICY` (path to the JSON file; re-read whenever its
  mtime changes, so an operator's edit applies to the next call without a restart) and `KUSD_SPEND_LEDGER`
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
- `allow_capabilities` on a direct `pay(provider_dict)` trusts the dict's own `capability`; it binds
  through `pay_best`, where the registry filters on the capability exactly. The payee allowlist and the
  quote-to-listed-key check are what bind the money either way.

## Calls made here

- Defaults in `k402-mcp` when no policy file is set (above). Chosen so a fresh install cannot lose
  more than a few dollars an hour by accident; the operator raises them by writing a policy file.
- `require_listed_payee=True` inside a policy, `False` behaviour kept when there is no policy.
- A non-`refused before signing` send error counts against the budget.
- `max_total_cents` has no reset command; the operator starts a new ledger file.
