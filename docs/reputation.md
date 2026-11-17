# Settlement-verified reputation

*Part of the Kaspa Agent Market trust layer, with [operator spending controls](spend-policy.md).*

A listing's reputation is **the sum of payments the registry has verified on a node**. Nobody enters a rating,
a provider cannot buy stars, and the registry cannot inflate a counter: every figure is recomputed from rows
that each name a transaction anyone can check.

## Where a reputation row comes from

1. An agent pays a provider for one call (kUSD box or a KAS channel close).
2. The provider reports the settlement to the registry while the box is unspent:
   `POST /registry/settled {payee_pubkey, txid, cents, payment_id, capability}`. The report is free and
   unsigned; it is only an invitation to look.
3. The registry derives the box's key (the listed key tweaked by the offer's `payment_id`, see
   [`k402/kusd_keys.py`](../k402/kusd_keys.py)), asks **its own node** for the UTXOs at that holder address,
   and keeps the row only if the node holds the box, with the family's covenant id, for exactly `cents`.
   A box the node does not see, a plain-KAS output at the same address, or the wrong amount is dropped.
4. The row (`settled_kusd`: payee, txid, cents, capability, time) is now part of the provider's record.

KAS payment channels follow the same shape with `settled_closes` (the close transaction checked on chain).

## What the registry publishes

Every listing in `GET /registry/search` and `GET /registry/provider/{payee_pubkey}` carries:

| field | meaning |
| --- | --- |
| `reputation.kusd_payments` | verified kUSD settlements |
| `reputation.settled_cents` | their sum, in cents |
| `reputation.closes`, `reputation.settled_kas` | verified KAS channel closes and their sum |
| `reputation.first_settled`, `reputation.last_settled` | unix seconds of the oldest and newest row |

`GET /registry/activity` lists the newest rows (the market page's live feed) and
`GET /registry/settled/{txid}` answers "did this payment count?" for one transaction. Field names and shapes
are pinned in [`openapi/registry.openapi.json`](../openapi/registry.openapi.json).

## How an agent checks a number itself

Trust, then verify, against a node you control:

```python
from k402.kusd_client import KusdPayer
payer = KusdPayer(wallet)                      # your KusdWallet (kusd_pay binary + your node)
row = (await payer.http.get(f"{registry}/registry/settled/{txid}")).json()
v = wallet.verify(txid, derive_payee(listed_key, payment_id), cents, wallet.family().authority, 0)
assert row["verified"] and v.ok                # the registry's answer and your node's agree
```

The MCP server does the same in one tool: `verify_settlement(txid, payee_pubkey, cents, payment_id)` returns
the registry's row and the node's verdict side by side.

## What reputation does not claim

- It counts **paid** calls, not **served** calls. A provider that takes a payment and answers nothing still
  earns the row; what stops an agent from paying it again is the operator's `max_window_failures` bound in its
  [spend policy](spend-policy.md).
- It is per listed key. A provider that re-lists under a new key starts from zero; a provider's history under
  one key is visible to every agent.
- It says nothing about quality. It says the provider has been paid, how much, how often, and when, with each
  payment checkable on chain.

## Why not ratings

Ratings can be written by anyone, including the provider. A settlement row needs a transaction that exists,
carries the kUSD covenant id, pays the derived key and matches the offer. Faking one costs the real payment,
the 1.5 KAS box deposit and the fee, and leaves a transaction with the provider's own listed key in it. The
registry is a cache of what the chain says; when a reader disagrees with the registry, the chain wins.
