# The kUSD market, v1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (chosen by the owner, inline) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** agents buy k402-listed services priced and settled in kUSD, over x402 V2 (kUSD as an asset of the Kaspa network) with k402's own header as the alias; providers verify every payment against their own node; proven live on TN10.

**Architecture:** a Rust binary `kusd_pay` (kusd-framework harness) does the chain work key-safely (send / verify / buy / balance, JSON in, JSON out); k402 (Python) wraps it as `KusdWallet`, offers kUSD on both wires (`X402Server` asset = authority covid; k402 scheme `kusd-utxo`), verifies by address against the node, lists in kUSD with `currency` / `price_cents`, ranks kUSD by `settled_cents`, and pays with `KusdPayer` (x402 first, k402 header fallback); k402-mcp exposes it to MCP agents.

**Tech Stack:** Rust (kusd-framework `harness`, `kusd-client`, kaspa gRPC), Python 3.12 (`~/k402/.venv`, httpx, FastAPI, pytest), k402-mcp.

**Spec:** `~/k402/DESIGN-kusd-market.md` (revised twice 2026-11-16). The spec wins on conflict.

## Global Constraints

- Exact amounts: a payment is a holder box of exactly `amount_cents` owned by `payTo`, family `asset` (authority covid). Verification: `kusd-client` `box_address` of `{amount, owner, 0x00, 0, borrow_guard, ext}` → node UTXOs at that address → entry with `outpoint.transactionId == txid`, `covenant_id == authority`, `blockDaaScore >= since_daa`; txid consumed once. REST API (`/addresses/{a}/utxos`) only as a configured fallback.
- x402 V2 requirement for kUSD: `scheme exact`, `network kaspa:testnet-10|kaspa:mainnet`, `amount` cents (string), `asset` authority covid hex, `payTo` payee x-only hex, `extra {assetSymbol: "kUSD", decimals: 2, assetTransferMethod: "submitted", payment_id, binding: "kusd-holder-v1"}`; payload proves with the txid as the KAS binding does. k402-native: scheme `kusd-utxo`, offer fields `amount_cents, pay_to_pubkey, authority, payment_id, expires, network`, header `kusd-utxo <txid> <payment_id>`.
- Listings: `currency ∈ {KAS, kUSD}` (default KAS), `price_cents` int; existing listings' canonical bytes unchanged (new fields only when set). Ranking: `currency=kUSD` → `settled_cents` then price; `currency=KAS` → as today; none → USD value, KAS at the kUSD oracle liq price from the relay (ordering only).
- Registry: kUSD listing fee 10 cents (TN10); delist and settled reports stay free; a settled report is verified by address and refused once the box is spent.
- Keys: `KUSD_KEY_FILE` mode 600, never printed; TN10 keys stay in `~/.kusd-v2-tn10/`; a new agent key for the proof lives in `~/.kusd-market/agent.key`. k402 tests never touch the network (fake bin / MockTransport). Accounts: gordonmurray-coding for kusd-framework, Kali123411 for k402 / k402-mcp (noreply author `64617127+Kali123411@users.noreply.github.com`). Merges are the owner's; nothing is pushed to a PR branch after it is announced ready. Heartbeat timer stopped around any manual `tn10_deploy` run; `EVIDENCE_DIR` outside the repo for hand-run stages. Zero open issues: review findings are fixed in the plan.
- Ports for the TN10 proof on murr7800: registry 8791, provider 8792 (free as of 2026-11-16).

## Review Focus

1. A payer pays the right amount to the right key but in the OLD kUSD family (a box under the previous authority): refused ("asset mismatch"), not served. Task 1 `verify_refuses_another_family`.
2. The same txid presented twice (two offers, one payment): the second is refused and no second call is served. Task 2 `replayed_txid_refused`.
3. The provider's node has not yet indexed the payment: the verifier says "retry shortly" (a 402 with the same offer), never "paid" and never a permanent failure. Task 2 `unindexed_payment_is_retry_not_failure`.
4. An x402 client that does not know the kUSD asset: it gets the KAS offer too (when the provider offers both) and nothing breaks. Task 2 `kas_and_kusd_offers_coexist`.
5. A settled report for a box the provider already spent: refused by the registry, with a reason the provider can act on ("report before spending"). Task 3 `settled_report_after_spend_refused`.

---

### Task 1: `kusd_pay` (kusd-framework, branch `feat/kusd-pay`, worktree `~/kusd-pay`)

**Files:**
- Create: `contracts/v2/harness/src/market.rs` (pure parts), `contracts/v2/harness/src/bin/kusd_pay.rs` (CLI: node + relay I/O), `contracts/v2/harness/tests/market.rs`.
- Modify: `contracts/v2/harness/src/lib.rs` (`pub mod market;`), `contracts/v2/README.md` (the bin), `contracts/v2/deploy/README.md` (operating note).

**Interfaces (Produces):**
- `market::Family { authority: Key, borrow_guard: Key, ext: Key }`; `market::family_from_authority(state: &KusdState, covid: Key) -> Family`.
- `market::holder_address(cents: i64, payee: Key, family: &Family, testnet: bool) -> Result<String, String>` (via `kusd_client::box_address(BoxKind::Holder, …)`).
- `market::find_payment(utxos: &[UtxoView], txid: &str, authority: Key, since_daa: u64) -> Result<Found { index: u32, daa: u64 }, String>` where `UtxoView { txid: String, index: u32, covenant_id: Option<Key>, daa: u64, amount: u64 }`; errors name the cause: `not indexed yet`, `asset mismatch`, `older than the offer`.
- `market::relay_protocol(json: &serde_json::Value) -> Result<ProtocolBoxes, String>` (parse the relay's `/state`), checked against the node by the bin as `tn10_deploy` does.
- CLI (`kusd_pay <cmd> [--json]`, env `KUSD_KEY_FILE`, `KUSD_NODE_URL` (default `grpc://192.168.4.38:16210`), `KUSD_RELAY_URL` (default `http://127.0.0.1:8735`), `KUSD_NETWORK` (`testnet-10`)):
  - `send --to <xonly> --cents N` → `{"txid","index","amount","change_cents"}`;
  - `verify --txid T --to <xonly> --cents N --authority <covid> --since-daa D [--api URL]` → `{"ok":true,"index","daa"}` | `{"ok":false,"reason"}` (exit 0 either way; exit 2 on usage);
  - `buy --cents N` → `{"txid","cents","kas_in","fee"}` (kusd-client `swap_in`, fee switch honoured);
  - `balance` → `{"kusd_cents","boxes":[{"txid","index","cents"}],"kas_sompi","address","xonly"}`;
  - `family` → `{"authority","borrow_guard","ext","network"}`.

- [ ] **Step 1: Failing tests** in `tests/market.rs`: `holder_address_matches_kusd_client` (same address as `kusd_client::box_address` for the golden fixture's holder), `find_payment_matches_exact_outpoint`, `verify_refuses_another_family` (RF 1: covenant id differs → `asset mismatch`), `find_payment_older_than_offer_refused`, `find_payment_not_indexed_is_retryable` (RF 3: no entry → error text contains `not indexed yet`), `relay_protocol_parses_the_relay_state` (fixture = the relay JSON shape from `relay_entries.rs`).
- [ ] **Step 2:** `cargo test --release --locked --test market` → FAIL (module missing).
- [ ] **Step 3:** implement `market.rs`; `cargo test --release --locked --test market` → PASS.
- [ ] **Step 4:** implement `bin/kusd_pay.rs` (reuse the driver's patterns: gRPC client, `relay_sync`-style adoption via `box_address` + UTXO check, `kusd_client::{send, swap_in}`, `preverify` before submit, key file mode check from `operator::load_key`); `cargo build --release --locked --bin kusd_pay` warning-free.
- [ ] **Step 5: Live smoke on TN10** (timer stopped; `EVIDENCE_DIR` unused by this bin): a fresh agent key in `~/.kusd-market/agent.key` (mode 600); `tn10_deploy STAGE=fund TO=<agent address> AMOUNT=2000000000` (20 KAS); `kusd_pay balance`; `kusd_pay buy --cents 50`; `kusd_pay send --to <deploy xonly> --cents 5`; `kusd_pay verify` of that txid → `ok:true`; `verify` with `--cents 6` → `ok:false`; against the old authority → `asset mismatch`. Record txids.
- [ ] **Step 6:** full suite green, warning-free; commit; PR (gordonmurray-coding); fresh-reviewer pass; fix everything it finds.

### Task 2: k402 — wallet wrapper, both wires, provider verification (branch `feat/kusd-market` in `~/k402`)

**Files:**
- Create: `k402/kusd.py` (`KusdWallet`, `KusdError`, `Family`); extend `k402/schemes.py` (`SCHEME_KUSD = "kusd-utxo"`, `KusdOffer` dataclass with `to_dict/from_dict`), `k402/x402.py` (`ASSET_KUSD` helpers: `kusd_requirements(network, amount_cents, pay_to_pubkey, authority, payment_id, …)`, `is_kusd(accepted)`), `scheme_exact_kusd.md`, tests `tests/test_kusd.py`, `tests/test_kusd_server.py`.
- Modify: `k402/server.py` (`K402(kusd: KusdWallet | None, kusd_price_cents: int | None)`: `_demand` appends a `KusdOffer`; `verify` branches on `SCHEME_KUSD` → `wallet.verify(txid, pay_to, cents, authority, since_daa)`; store consumes txids), `k402/x402_server.py` (`X402Server(kusd: KusdWallet | None, kusd_price_cents …)`: kUSD requirement in `demand`, `_check` dispatch on `accepted.asset == family.authority` → wallet verify), `k402/store.py` (txid set: `mark_txid_used(txid) -> bool`), `PROTOCOL.md` §4 `kusd-utxo`, `README.md` (kUSD section).

**Interfaces:**
- Consumes: Task 1 CLI JSON shapes.
- Produces: `KusdWallet(bin_path, key_file, node_url=None, relay_url=None, network="testnet-10", api_fallback=None)` with `family() -> Family`, `send(to_xonly, cents) -> dict`, `verify(txid, to_xonly, cents, authority, since_daa) -> VerifyResult(ok, reason, index, daa, retryable)`, `buy(cents) -> dict`, `balance() -> dict`; `KusdOffer(network, amount_cents, pay_to_pubkey, authority, payment_id, expires, description)`; `x402.kusd_requirements(...)`.

- [ ] **Step 1: Failing tests** (fake `kusd_pay` = a Python script in `tests/fake_kusd_pay.py` driven by env `FAKE_KUSD_RESULT`): `test_kusd.py`: `wallet_parses_bin_json`, `wallet_refuses_key_file_not_600`, `verify_result_retryable_when_not_indexed`; `test_kusd_server.py` (FastAPI TestClient): `kusd_offer_in_402_on_both_wires` (k402 body has `kusd-utxo`; `PAYMENT-REQUIRED` has `exact` with the kUSD asset), `kas_and_kusd_offers_coexist` (RF 4), `kusd_payment_served_once`, `replayed_txid_refused` (RF 2), `unindexed_payment_is_retry_not_failure` (RF 3: 402 again with the same payment_id), `wrong_amount_refused`.
- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_kusd.py tests/test_kusd_server.py -q` → FAIL.
- [ ] **Step 3:** implement; same command → PASS; whole suite `.venv/bin/python -m pytest -q` → 74 + new.
- [ ] **Step 4:** docs (`PROTOCOL.md` §4, `scheme_exact_kusd.md`, README); commit (Kali123411 author), push, PR. Fresh-reviewer pass; fix everything.

### Task 3: k402 — listings, ranking, settled reports in kUSD

**Files:**
- Modify: `k402/registry.py` (`Listing.currency`, `price_cents`; `to_dict` emits them only when `currency != "KAS"`; `RegistryClient.search(currency=)`), `k402/registry_server.py` (search filter + ranking per Global Constraints; `/registry/settled` accepts `{txid, cents, payee_pubkey}` verified through `KusdWallet.verify`, adds `settled_cents`; kUSD listing fee via `K402(kusd=…)`), `tests/test_registry.py`.

- [ ] **Step 1: Failing tests:** `kusd_listing_round_trips_and_keeps_kas_signatures_unchanged`, `search_filters_by_currency`, `kusd_ranking_by_settled_cents`, `mixed_ranking_uses_usd_value_at_oracle_price` (oracle price injected), `settled_report_in_cents_verified`, `settled_report_after_spend_refused` (RF 5: fake verify says `not indexed`/spent → 409 with "report before spending"), `kusd_listing_fee_required`.
- [ ] **Step 2:** FAIL → **Step 3:** implement → PASS; suite green; commit; push to the same PR as Task 2 (not yet announced ready).

### Task 4: k402 — `KusdPayer` and the MCP tools

**Files:**
- Create: `k402/kusd_client.py` (`KusdPayer(wallet, registry_url, network, http=None)`: `discover(currency="kUSD")`, `rank`, `preflight`, `ensure_funds(cents, headroom_cents=50)`, `pay(provider, path, json_body, cents=None)`, `pay_best(capability, …)` returning `RouteResult`; x402 first: decode `PAYMENT-REQUIRED`, pick `exact` with `asset == family.authority`, `wallet.send`, `PAYMENT-SIGNATURE`; fallback: k402 body `kusd-utxo` offer + `X-K402-Payment`), `tests/test_kusd_client.py` (httpx MockTransport).
- Modify: `~/k402-mcp/k402_mcp.py` (`registry_search(currency=)`, new tool `pay_kusd(capability, body, max_price_cents)`), its README, version bump.

- [ ] **Step 1: Failing tests:** `payer_pays_x402_kusd_offer`, `payer_falls_back_to_k402_header`, `payer_tops_up_from_psm_when_short`, `payer_reports_full_outlay` (price + 1.5 KAS + 0.1 KAS), `pay_best_fails_over`.
- [ ] **Step 2:** FAIL → **Step 3:** implement → PASS; suite green; commit; push; **now announce the k402 PR ready** (Tasks 2–4) after a fresh-reviewer pass and fixes. k402-mcp PR separately.

### Task 5: TN10 proof

- [ ] **Step 1:** `kusd_pay` installed to `~/.local/share/kusd-market/bin/` from `~/kusd-main` after Task 1's merge (strings-check).
- [ ] **Step 2:** registry on murr7800 `:8791` (`create_registry_app` with `K402(kusd=provider wallet)`, kUSD listing fee 10¢), provider on `:8792` (the demo summarize service from `~/k402/demo` or `examples/`, `K402(kusd=…, kusd_price_cents=5)`), both as systemd user units with env files (keys mode 600). Provider lists `summarize` in kUSD, paying the 10¢ fee in kUSD through k402.
- [ ] **Step 3:** agent (`KusdPayer` with `~/.kusd-market/agent.key`): `ensure_funds` buys kUSD from the PSM; `pay_best("summarize")` → served; registry `settled_cents` +5; replay / 1¢ short / old-family / spent-box-report all refused as RF 1–5 say.
- [ ] **Step 4:** records: k402 `DEMO-kusd-market.md` (txids, headers, refusals), kusd-framework `docs/PARAMETER-REVIEW.md` one line (first native kUSD demand), memory. PRs: k402 (Tasks 2–4 + demo), k402-mcp, kusd-framework records.
