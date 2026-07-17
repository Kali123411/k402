# `kaspa-channel` — covenant-enforced payment channels (k402 v0.2 draft)

Fills the scheme reserved in PROTOCOL.md §4: unidirectional payment channels with
consensus-enforced settlement on Kaspa L1. Per-call granularity, zero per-call chain
latency, two on-chain transactions per channel (open + close), no custodian and no
facilitator anywhere in the money path.

## Why this beats the alternatives

| | kaspa-session (today) | x402 (Base) | kaspa-channel |
|---|---|---|---|
| Merchant holds float | yes (custodial) | no, but facilitator settles | **no — covenant box** |
| Per-call chain latency | none | ~seconds (L2 + facilitator) | **none** |
| Merchant theft bound | whole balance | n/a | **exactly the signed total** |
| Payer rug bound | n/a | n/a | **zero — voucher is claimable** |
| Trust in third party | merchant | facilitator + sequencer | **none (L1 consensus)** |
| Sub-cent per-call pricing | yes | yes | **yes** (no per-call dust floor) |
| Chain txs per N calls | 1 deposit | N settles | **2** |

## The covenant (`channel.sil`)

```
contract Channel(pubkey payer, pubkey payee, int expiryDaa, int maxFee) {
    // CLOSE — the payee presents the payer's latest off-chain voucher. Consensus
    // enforces the exact split: `total` to the payee, remainder back to the payer,
    // in one tx. Closing early or late changes nothing about the split.
    entrypoint function close(sig payeeSig, datasig voucher, int total) {
        require(checkSig(payeeSig, payee));
        require(total > 0);
        // the voucher binds THIS channel instance and the cumulative total:
        //   msg = H(covenant_id || i64le(total)); voucher = payer's BIP340 sig over msg
        // covenant_id is read dynamically (OpInputCovenantId) — vouchers cannot
        // replay across channels, even between the same payer/payee pair.
        <verify CSFS(voucher, H(cid || total), payer)>          // exact builtins TBD vs compiler source
        require(tx.outputs[0].value == total);                   // payee payout, P2PK(payee)
        require(tx.outputs[1].value == input.value - total - maxFee);  // remainder, P2PK(payer)
    }
    // REFUND — at/after expiry the payer reclaims everything unclaimed.
    entrypoint function refund(sig payerSig) {
        require(OpTxInputDaaScore(this.activeInputIndex) >= expiryDaa);
        require(checkSig(payerSig, payer));
        <everything - maxFee back to P2PK(payer)>
    }
}
```

A voucher is a bare 64-byte schnorr signature — any secp256k1 library produces one;
no chain interaction, no wallet round-trip. Monotonicity needs no enforcement: the
payee only ever benefits from the highest total, so it keeps the max it has seen.

## Protocol surface

402 offer entry:
```json
{"scheme": "kaspa-channel", "network": "mainnet",
 "payee_pubkey": "<x-only hex>", "price_sompi": "3000000",
 "min_channel_sompi": "100000000", "max_channel_sompi": "500000000",
 "min_expiry_daa_delta": 86400, "open": "/channel/open"}
```

Open: the client compiles the channel covenant (payer = its key, payee from the offer),
funds it on mainnet, then `POST /channel/open {outpoint}`. The server independently
recompiles with the same ctor args and verifies on-chain: template hash, value, expiry
sanity → returns `channel_id` (the covenant id).

Per call: `X-K402-Payment: kaspa-channel <channel_id> <total_sompi> <voucher_hex>`
The server checks: known channel; `total - highest_seen ≥ price`; `total ≤ value - maxFee`;
schnorr verifies against the payer key. All in-memory — then serves. ~microseconds.

Close: the payee broadcasts `close` with the latest voucher whenever it likes (policy:
approaching expiry, or claimable ≥ threshold). The remainder returns to the payer in
the same transaction — no separate refund step needed unless the payee never claims.

## Deployment honesty

- Covenant is UNAUDITED. Mainnet launch gated behind small caps (`max_channel_sompi`
  ≈ 5 KAS) and the scheme marked experimental in the catalog.
- The payee "hot" key on the settlement service can only execute `close` — worst-case
  compromise claims exactly what vouchers authorize, to the payee's own address.
- Key technical verification before build (per repo discipline — real source, not docs):
  in-script hash over concat (blake2b/sha256 builtin) + CSFS message construction, and
  KIP-9 storage-mass floors for the close tx's two outputs.

## Build phases

1. `channel.sil` + debugger tests (join_tests style) → live TN10 open/voucher/close/refund cycle.
2. `k402` package: scheme in client (voucher signer) + server middleware (verify); channel
   manager in the settlement service (register/verify/close, payee key).
3. Gateway offers `kaspa-channel`, MCP tools (`open_channel` etc. — covenant build/broadcast
   via the existing covenants-as-a-service), PROTOCOL.md v0.2, mainnet soft launch with caps.
