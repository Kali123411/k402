# Operator spending controls (SpendPolicy)

*Part of the Kaspa Agent Market trust layer, with [settlement-verified reputation](reputation.md).
Design notes: [`DESIGN-kusd-spend-controls.md`](../DESIGN-kusd-spend-controls.md).*

An autonomous agent holds a kUSD wallet and pays per call. **The operator, not the agent, decides how much it
may spend**, and the limits are enforced in the payer before any box is built: a refused payment moves nothing.
Every bound is checked and recorded atomically in a local ledger (sqlite), so two concurrent calls cannot both
fit under the same remaining budget.

## The policy

A JSON file the operator writes and may edit while the agent runs (the payer re-reads it per payment):

```json
{
  "max_per_call_cents": 10,
  "max_total_cents": 500,
  "window_seconds": 3600,
  "max_window_cents": 100,
  "max_window_calls": 20,
  "max_window_topup_cents": 0,
  "max_window_failures": 2,
  "allow_payees": null,
  "allow_capabilities": ["summarize", "embed"],
  "require_listed_payee": true
}
```

| bound | refusal code | what it stops |
| --- | --- | --- |
| `max_per_call_cents` | `per_call` | one call priced above the cap |
| `max_total_cents` | `total` | the ledger's lifetime total going past the budget |
| `max_window_cents` | `window_cents` | kUSD sent in the last `window_seconds` |
| `max_window_calls` | `window_calls` | paid calls in the window |
| `max_window_topup_cents` | `topup` | kUSD bought through the PSM in the window (`0` = the agent never swaps KAS for kUSD) |
| `max_window_failures` | `failures` | paid-but-unserved calls in the window; at the limit every payment stops until the window passes or the operator intervenes |
| `allow_payees` | `payee` | paying any listed key not on the list |
| `require_listed_payee` | `unlisted_payee` | paying an endpoint the registry does not list |
| `allow_capabilities` | `capability` | paying for a capability not on the list (a bare endpoint with no capability is refused too) |
| freeze (ledger flag) | `frozen` | everything: the kill switch |

`null` means unbounded for that bound. A refusal carries its code, the reason in words, and the ledger status.

## The ledger

Each payment is reserved before the box is built and settled or voided after:

- **reserve** checks every bound and records the cents as spent, in one transaction;
- **settle** records the txid and the HTTP status the provider answered (0 = no answer); an unserved paid call
  counts toward `failures`;
- **void** releases a reservation that never left the wallet (quote refused, payee mismatch, `KusdNotRun`).

What was paid, to whom, for what and whether it was served is therefore always in the ledger, with the txid.

## Operating it

```bash
k402-spend --ledger ~/.kusd-market/agent-spend.db --policy agent-policy.json status
k402-spend --ledger ~/.kusd-market/agent-spend.db freeze      # kill switch: every payment refused
k402-spend --ledger ~/.kusd-market/agent-spend.db unfreeze
k402-spend --ledger ~/.kusd-market/agent-spend.db history --limit 10
```

The same views exist as MCP tools (`kusd_spend_status`, and every paid tool returns the ledger's remaining
figures beside its result) and on the market page's operator drawer for the browser wallet.

## In code

```python
from k402.kusd_client import KusdPayer
from k402.spend import SpendLedger, SpendPolicy

policy = SpendPolicy.load("agent-policy.json")             # or SpendPolicy(max_per_call_cents=10, ...)
payer = KusdPayer(wallet, ledger=SpendLedger("agent-spend.db"), policy=policy)
res = await payer.pay_best("summarize", json_body={"text": "..."}, max_price_cents=10)
```

`pay_best` discovers, ranks by reputation, quotes, checks the policy, and only then pays; a `SpendRefused`
names the bound that stopped it.

## What the policy does not do

- It does not limit what the agent *asks*; it limits what the agent *pays*. A free request is never counted.
- It does not protect the KAS side of the wallet except through `max_window_topup_cents`: box deposits
  (1.5 KAS per payment, recovered by the provider) and fees are spent with every kUSD payment.
- It is local to the agent's ledger. Two agents sharing a wallet need to share the ledger file too, or each
  one's budget is counted alone.
