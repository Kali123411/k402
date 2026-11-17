# Operator spending controls — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline). Steps use `- [ ]`.

**Goal:** an operator-written `SpendPolicy`, enforced by `KusdPayer` through a persistent `SpendLedger`, so a kUSD-market agent can only spend what it was allowed.

**Architecture:** new module `k402/spend.py` (policy, ledger, refusal, operator CLI); `KusdPayer.pay`/`pay_best`/`ensure_funds` reserve in the ledger before any money moves; `k402-mcp` wires the policy from env with a conservative default and adds a read-only status tool.

**Tech stack:** Python 3.12, sqlite3, pytest(-asyncio auto), httpx MockTransport (existing test harness in `tests/test_kusd_client.py`).

**Spec:** `DESIGN-kusd-spend-controls.md`.

## Global constraints

- Tests: `cd ~/k402-spend && ~/k402/.venv/bin/python -m pytest -q` (whole suite must stay green, 124 before this plan).
- No chain, no real binary in tests (`FakeWallet`, `fake_kusd_pay.py`).
- Commits by Kali123411 (`git -c user.name=Kali123411 -c user.email=64617127+Kali123411@users.noreply.github.com commit`).
- Zero warnings, every review finding fixed in the same plan.

## Review focus

1. Two concurrent `pay()` calls against one remaining budget must not both pass → Task 2 threads test.
2. A `wallet.send` that raises after broadcasting must stay counted → Task 3 test with a send that raises "relay publish failed".
3. The agent passing `max_price_cents` above the policy's per-call ceiling must get the policy's ceiling → Task 3.
4. A top-up that would exceed `max_window_topup_cents` must be refused *before* `wallet.buy`, and with `0` the wallet must never buy → Task 3.
5. `pay_best` on a budget refusal must not try the next provider → Task 3.

---

### Task 1: SpendPolicy and SpendRefused

**Files:** Create `k402/spend.py`, `tests/test_spend.py`.

**Produces:** `SpendPolicy` frozen dataclass with the spec's fields (`max_per_call_cents`, `max_total_cents`, `window_seconds=3600`, `max_window_cents`, `max_window_calls`, `max_window_topup_cents`, `max_window_failures`, `allow_payees: Optional[frozenset]`, `allow_capabilities: Optional[frozenset]`, `require_listed_payee=True`); `SpendPolicy.from_dict(d)`, `.to_dict()`, `SpendPolicy.load(path)` (JSON file); `.cap(agent_cap: Optional[int]) -> Optional[int]` = min of the two ignoring None; `SpendRefused(KusdError)` with `.code: str` and `.status: dict`.

- [ ] Step 1: tests `test_policy_from_dict_roundtrip_and_sets`, `test_policy_rejects_negative_and_zero_window` (ValueError), `test_policy_cap_is_the_tighter_of_agent_and_policy` (`cap(None)==25`, `cap(10)==10`, `cap(50)==25`, no-limit policy `cap(7)==7`, `cap(None) is None`), `test_policy_load_reads_json_file`, `test_spend_refused_is_a_kusd_error_with_a_code`.
- [ ] Step 2: run → FAIL (ImportError).
- [ ] Step 3: implement in `k402/spend.py`.
- [ ] Step 4: run → PASS.
- [ ] Step 5: commit `feat(spend): SpendPolicy, the operator's bounds on a kUSD agent`.

### Task 2: SpendLedger

**Files:** Modify `k402/spend.py`, `tests/test_spend.py`.

**Produces:** `SpendLedger(path: str)` (sqlite, `":memory:"` allowed, `check_same_thread=False`, one connection guarded by a lock; schema `spend(id, ts, kind, provider, capability, cents, kas_sompi, txid, http, state)` + `spend_control(key, value)`); `reserve(policy, *, kind: "pay"|"topup", cents, provider="", capability="", kas_sompi=0, now=None) -> int` (one `BEGIN IMMEDIATE` transaction: frozen → `SpendRefused("frozen")`; failures ≥ max → `"failures"`; kind pay: total+cents > max_total → `"total"`, window cents → `"window_cents"`, window calls → `"window_calls"`; kind topup: window topup cents → `"topup"`; then INSERT state `reserved`); `settle(id, *, txid="", http=None, kas_sompi=None)` → state `sent`; `void(id)`; `freeze()/unfreeze()/frozen`; `status(policy, now=None) -> dict` with `frozen, total_cents, window: {seconds, cents, calls, topup_cents, failures}, remaining: {per axis or None}`; `history(limit=20) -> list[dict]` newest first. Counting excludes `void`; failures = pay rows with state `sent` and `http >= 400`.

- [ ] Step 1: tests `test_ledger_reserve_counts_toward_total_and_window`, `test_ledger_refuses_each_axis_with_its_code` (total, window_cents, window_calls, topup, failures, frozen; each leaves no new row), `test_ledger_void_frees_the_reservation`, `test_ledger_window_slides` (rows at now-3601 do not count), `test_ledger_status_and_history`, `test_ledger_concurrent_reserves_never_overspend` (8 threads × reserve 10¢ against `max_total_cents=50` → exactly 5 succeed).
- [ ] Step 2: run → FAIL. Step 3: implement. Step 4: PASS.
- [ ] Step 5: commit `feat(spend): SpendLedger — the persistent memory the bounds are measured against`.

### Task 3: KusdPayer enforces the policy

**Files:** Modify `k402/kusd_client.py`, `tests/test_kusd_client.py`.

**Consumes:** Task 1/2 API. **Produces:** `KusdPayer(..., policy: Optional[SpendPolicy]=None, ledger: Optional[SpendLedger]=None)` (policy without ledger → `ValueError`); `pay()` applies `policy.cap(max_price_cents)`, then after `_check` the policy checks (frozen handled by reserve; capability from `provider.get("capability")`; payee in `allow_payees`; `require_listed_payee`), `ensure_funds(cents, provider, capability)` reserves kind `topup` for the buy amount before `wallet.buy` and settles with `kas_sompi = kas_in + fee`; `pay()` reserves kind `pay` (kas_sompi = `Outlay.kas_sompi`) before `wallet.send`, voids on a `KusdError` whose text contains `refused before signing`, otherwise settles with txid and later the final `http`; `discover()` drops payees not in `allow_payees`; `pay_best()` re-raises `SpendRefused`; `spend_status()`.

- [ ] Step 1: tests `test_payer_policy_requires_a_ledger`, `test_policy_cap_overrides_a_looser_agent_cap` (policy 10¢, agent 50¢, quote 20¢ → refused "above the cap", nothing sent), `test_policy_budget_refuses_before_sending_and_is_counted_after` (max_total 12, two 5¢ pays ok, third `SpendRefused` code total, ledger total 10), `test_policy_topup_zero_never_buys` (short wallet, `max_window_topup_cents=0` → SpendRefused code topup, `w.bought == []`, `w.sent == []`), `test_policy_topup_is_reserved_before_buying_and_recorded`, `test_policy_payment_sent_counts_and_trips_the_failure_limit` (fail_after_payment provider, `max_window_failures=1`: first pay raises PaymentSent, second raises SpendRefused code failures without sending), `test_policy_send_error_after_broadcast_stays_counted` (wallet.send raises KusdError("relay publish failed") → row kept, status total == cents; raises KusdError("refused before signing: …") → voided), `test_policy_allow_payees_filters_discovery_and_refuses_direct_pay`, `test_policy_requires_a_listed_payee`, `test_policy_allow_capabilities`, `test_pay_best_does_not_fail_over_on_a_spend_refusal` (two providers, max_total 3, quotes 5 → SpendRefused, neither paid), `test_payer_without_policy_is_unchanged` (bare endpoint still paid).
- [ ] Step 2: FAIL. Step 3: implement. Step 4: PASS, whole suite green.
- [ ] Step 5: commit `feat(kusd_client): KusdPayer spends only inside the operator's SpendPolicy`.

### Task 4: operator CLI, exports, version

**Files:** Modify `k402/spend.py` (add `main(argv) -> int`, `if __name__ == "__main__"`), `k402/__init__.py` (`SpendPolicy`, `SpendLedger`, `SpendRefused`; `__version__ = "0.10.0"`), `pyproject.toml` (0.10.0), `tests/test_spend.py`.

- [ ] Step 1: tests `test_cli_status_freeze_unfreeze_history` (runs `main([...])` with a tmp ledger + policy json, capsys: `status` prints JSON with `frozen`, `freeze` → status frozen true, `unfreeze` → false, `history --limit 1` prints one row), `test_cli_status_without_policy_still_works`, `test_package_exports_and_version` (`k402.__version__ == "0.10.0"` and matches pyproject).
- [ ] Step 2: FAIL. Step 3: implement. Step 4: PASS.
- [ ] Step 5: commit `feat(spend): operator CLI (status / freeze / unfreeze / history); k402 0.10.0`.

### Task 5: examples and docs

**Files:** Modify `examples/kusd_agent.py` (env `KUSD_SPEND_POLICY`, `KUSD_SPEND_LEDGER`; prints `spend_status()` after the call), `README.md` (section "Operator spending controls" under the kUSD market), `DESIGN-kusd-market.md` §5 (one paragraph pointing at the new design), `DEMO-kusd-market.md` (TN10 proof once run: a refused over-budget call).

- [ ] Step 1: write docs; run `examples/kusd_agent.py` against the live TN10 units with a 1¢ `max_total_cents` policy → expect `SpendRefused` before any send, then with a sane policy → one paid call and a status print. Record in the demo doc.
- [ ] Step 2: commit `docs: operator spending controls`.

### Task 6: k402-mcp

**Files (repo `~/k402-mcp`, branch `feat/kusd-spend-controls`):** Modify `k402_mcp.py` (`_kusd()` builds `SpendPolicy` from `KUSD_SPEND_POLICY` or `DEFAULT_SPEND_POLICY = {"max_per_call_cents": 25, "max_window_cents": 200, "max_window_calls": 20, "max_window_topup_cents": 100, "max_window_failures": 2}`, `SpendLedger(KUSD_SPEND_LEDGER or ~/.kusd-market/spend.db)`; `pay_kusd` returns `{"error", "code"}` on `SpendRefused`; new tool `kusd_spend_status`), `README.md`, `pyproject.toml` (0.9.0, `kusd = ["k402>=0.10"]`), create `tests/test_kusd_tools.py`.

- [ ] Step 1: tests (monkeypatch `k402_mcp._kusd_payer` with a `KusdPayer` over the FakeWallet + mock transport from k402's tests, or a stub with `pay_best`/`spend_status`): `test_pay_kusd_reports_a_spend_refusal_with_its_code`, `test_kusd_spend_status_tool`, `test_default_policy_applies_without_a_policy_file`.
- [ ] Step 2: FAIL. Step 3: implement. Step 4: PASS (`~/k402/.venv/bin/python -m pytest -q` in `~/k402-mcp`).
- [ ] Step 5: commit `feat: pay_kusd honours the operator's SpendPolicy; kusd_spend_status; 0.9.0`.

### Final

Fresh-reviewer pass on both branches, fix everything, push as Kali123411, open PRs (k402, k402-mcp), update memory. No PyPI release (owner's hold until the kUSD mainnet flip).
