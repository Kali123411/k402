# Exact Payment Scheme for kUSD on Kaspa (`exact`, asset = kUSD)

> Companion to `scheme_exact_kaspa.md`. kUSD is a covenant-native USD stablecoin on Kaspa
> (kusd-framework, `contracts/v2`): a kUSD box is a covenant UTXO whose state (amount in cents,
> owner key) sits behind its P2SH script hash and whose covenant id names the token family.

## Scheme Name

`exact` on `network: kaspa:mainnet` / `kaspa:testnet-10`; **`asset` = the kUSD authority's covenant id**
(64 hex), the token's on-chain identity (as a USDC asset is its contract address);
`extra.binding = "kusd-holder-v1"`.

## PaymentRequirements

```json
{ "scheme": "exact", "network": "kaspa:testnet-10", "amount": "5",
  "asset": "<authority covenant id>", "payTo": "<payee x-only hex>", "maxTimeoutSeconds": 120,
  "extra": { "assetSymbol": "kUSD", "decimals": 2, "assetTransferMethod": "submitted",
             "payment_id": "p_…", "binding": "kusd-holder-v1" } }
```

`amount` is in cents (decimal string). `payTo` is the payee's x-only key, which owns the kUSD box the
payer creates (not a KAS address). `extra.payment_id` correlates the requirement with the server's
record; `assetTransferMethod: "submitted"` means the payer broadcasts the transaction itself and proves
it with the txid, as the Kaspa `exact` binding does.

## PaymentPayload

```json
{ "x402Version": 2, "accepted": { …the requirement… },
  "payload": { "transaction": "<txid>", "payer": "<payer x-only hex, optional>" } }
```

## Paying

The payer's wallet builds a kUSD `transfer` creating one holder box of **exactly** `amount` cents
owned by `payTo` (kusd-client `send`; change returns to the payer) and broadcasts it.

## Verification (facilitator / server, against its own node)

1. Recompute the holder box's address: the P2SH of the holder state `{amount, owner: payTo,
   owner_scheme 0x00, borrow_scheme 0, borrow_guard 0, ext}` where `ext` is the family's gate
   commitment (read once from the authority box).
2. Ask the node for the UTXOs at that address; require an entry whose outpoint's transaction is the
   payload's `transaction`, whose **covenant id equals `asset`**, and whose accepting DAA score is at or
   after the requirement's creation.
3. Consume the txid: the same transaction never settles two requirements.

Only the exact amount is recognisable (the amount is inside the hash); a box of another amount is
indistinguishable from a payment not yet indexed, so the server answers "retry" until the
requirement expires. Plain KAS sent to the same address carries no covenant id and is refused. No
public index lists covenant ids, so verification needs a node (the public REST API cannot stand in).

## Settlement

Observation only: the payer already settled by broadcasting. `SettleResponse.transaction` is the
txid, `amount` the cents.

## Economics stated plainly

Every kUSD box must hold ≥ 1 KAS of KAS (a new one carries 1.5 KAS), which the recipient owns too and
recovers when boxes are merged or spent; the payer's outlay per call is `amount + 1.5 KAS + 0.1 KAS
fee`. Per-call kUSD suits calls of a few cents or more; sub-cent streaming is for kUSD channels
(phase 2 in `DESIGN-kusd-market.md`).
