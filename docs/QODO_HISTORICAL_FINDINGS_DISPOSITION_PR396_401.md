# Maintainer disposition of historical Qodo findings: PRs 396–401

**Repository:** `Papajona/Atlas-Bot`  
**Historical PRs:** #396, #397, #398, #399, #400, #401  
**Current-main baseline reviewed:** `fd5ba96967fc4e7f94aa9ff5aedb2e567e48b3cc`  
**Disposition:** Atlas-Bot maintainers  
**Purpose:** Record the code status of each historical finding, independently of Qodo's stale comments on closed pull requests.

## Authority

Qodo is an advisory code-review tool. It has no authority to merge code, deploy the application, change trading or payout settings, decide application completion, or certify production readiness. Its findings are not a release gate. Merges and releases remain governed by exact-commit repository checks, mandatory PostgreSQL verification, Production Core Verification, security checks, and separately approved production evidence.

Qodo has stated in its PR conversations that it cannot re-review a closed PR against a different `main` commit. The available GitHub integration cannot rewrite bot-authored review comments or update Qodo's external finding catalog. Thus the old “active” badges remain historical snapshots; they do not establish that the current code still contains the issue. This maintainer ledger is the repository's authoritative code disposition. It does not pretend to edit or replace Qodo's original comments.

The follow-up change associated with this ledger must pass the required exact-commit checks before the newly fixed findings are called merged. A passing test or green workflow is evidence for code; it does not itself authorize live trading or production release.

## Summary

| PR | Original Qodo review | Findings in snapshot | Qodo marked active in snapshot | Maintainer disposition |
|---|---|---:|---:|---|
| [#396](https://github.com/Papajona/Atlas-Bot/pull/396) | comment `6858617806` | 6 | 3 | Three old findings fixed in merged #398; three already marked resolved by Qodo. |
| [#397](https://github.com/Papajona/Atlas-Bot/pull/397) | comment `6859652257` | 8 | 0 | All eight already marked resolved by Qodo. |
| [#398](https://github.com/Papajona/Atlas-Bot/pull/398) | comment `6860270003` | 2 | 0 | Both already marked resolved by Qodo. |
| [#399](https://github.com/Papajona/Atlas-Bot/pull/399) | comment `6862532377` | 1 | 1 | Fixed by merged #405; its fresh Qodo review reported zero material issues. |
| [#400](https://github.com/Papajona/Atlas-Bot/pull/400) | comment `6862978439` | 11 | 2 | Both historical active findings are implemented and covered by behavioral/PostgreSQL regressions. |
| [#401](https://github.com/Papajona/Atlas-Bot/pull/401) | comment `6866337821` | 18 | 7 | All seven historical active findings are now accounted for: #2, #5, #6 and #18 are implemented in current main; #1, #3 and #4 are fixed in this follow-up. Findings #7–17 were already marked resolved by Qodo. |

## PR396 — withdrawal replay integrity

Original review: [#396](https://github.com/Papajona/Atlas-Bot/pull/396), Qodo comment `6858617806`.

| # | Historical finding | Maintainer disposition | Evidence |
|---:|---|---|---|
| 1 | Incident outages strand customer reserves | Fixed by merged #398 | Incident persistence runs in a SAVEPOINT in the caller's still-open transaction. The confirmed terminal withdrawal state and HIGH incident commit together. Regression: `tests/test_withdrawal_ledger_repair_db.py::test_replay_incident_write_failure_rolls_back_terminal_withdrawal_too`. |
| 2 | Ledger conflicts strand withdrawal reserves | Qodo marked resolved | Authorized manual ledger-repair endpoint and verified recovery path. |
| 3 | Concurrent mismatch replays crash after the incident is saved | Fixed by merged #398 | Duplicate-key writes are isolated in a SAVEPOINT and retried without rolling back/expiring caller ORM state. Regression: `test_duplicate_replay_incident_race_preserves_async_session_objects`. |
| 4 | Repair cannot release reserves for a real replay conflict | Fixed by merged #398 | Repair validates the corrected original journal and customer ledger; it does not repeat the same failed idempotent posting. Regressions: `test_repair_refuses_a_real_unfixed_replay_journal`, `test_rejected_terminal_withdrawal_has_authorized_ledger_repair`, and `test_repair_rejects_valid_journal_when_customer_reserve_is_still_locked`. |
| 5 | Orphaned releases accept changed amounts | Qodo marked resolved | Missing source-withdrawal identity fails closed in replay validation. |
| 6 | Mismatch incidents are lost on SQLite | Qodo marked resolved | Replay incidents use the caller's transaction rather than a separate SQLite writer. |

Fix commit: PR398 merge `88411c1f86383931b80a9f2a377f1a6a8c610e5c`.

## PR397 — shared execution-cost kernel

Original review: [#397](https://github.com/Papajona/Atlas-Bot/pull/397), Qodo comment `6859652257`. Qodo marked all eight findings resolved. Its later exact-head response `6861686189` explicitly confirmed findings 7 and 8 resolved on `fbeba7a7739d020af47df3642ebfac02c5d31e36`.

1. Locked installs still ship the vulnerable OpenTelemetry API — Qodo marked resolved.
2. Test wipes other platform positions in the shared DB — Qodo marked resolved.
3. Stop-trigger test now passes vacuously — Qodo marked resolved.
4. Invalid cost attempts get misreported — Qodo marked resolved.
5. Missing closes abort label construction — Qodo marked resolved.
6. Model training slows on long histories — Qodo marked resolved.
7. Invalid-cost test disables the position cap instead of isolating state — Qodo marked resolved and later confirmed by Qodo on the exact PR head.
8. Cost-context test skips the real invalid-input path — Qodo marked resolved and later confirmed by Qodo on the exact PR head.

## PR398 — withdrawal repair follow-up

Original review: [#398](https://github.com/Papajona/Atlas-Bot/pull/398), Qodo comment `6860270003`. Qodo marked both findings resolved.

1. Customers' repaired funds remain locked — resolved; repair checks journal-derived customer ledger buckets before wallet synchronization.
2. Valid repairs can fail on SQLite rounding — resolved; SQLite ledger reconciliation accumulates the journal values as exact decimals rather than relying on floating-point SQL aggregation.

## PR399 — training-label replay gate

Original review: [#399](https://github.com/Papajona/Atlas-Bot/pull/399), Qodo comment `6862532377`.

1. Replay gate no longer lists the label fix as open work — superseded by merged [#405](https://github.com/Papajona/Atlas-Bot/pull/405). `train_model()` calls `_encode_triple_barrier_labels()`, which rejects missing/non-finite/out-of-domain labels and maps `{-1, 0, 1}` to classifier classes `{0, 1, 2}`. The OANDA replay guide requires separate training-label and sparse/reordered prediction-probability tests.

Regression coverage:
- `tests/test_core.py::test_triple_barrier_training_labels_encode_as_classifier_classes`
- `tests/test_core.py::test_predict_latest_maps_sparse_reordered_class_probabilities`

PR405 merge commit: `7dedd403f8c7e01dc72364061b1c5a81033cbb48`. Qodo's review comment `6875865841` on PR405 reported 0 bugs, 0 rule violations and 0 requirement gaps.

## PR400 — customer-paper risk budgets and lock handling

Original review: [#400](https://github.com/Papajona/Atlas-Bot/pull/400), Qodo comment `6862978439`. Qodo marked nine of eleven findings resolved; findings 2 and 8 were historically active.

| # | Historical finding | Maintainer disposition | Evidence |
|---:|---|---|---|
| 1 | A regression test fails after mark loading changes | Qodo marked resolved | Review disposition in the original PR. |
| 2 | Reversals can exceed shared buy budgets | Implemented in current main | SELL reversals acquire the customer risk-budget lock, require fresh verified marks, calculate projected shared exposure using the conservative higher of mark and booked entry, and reject a cap breach before appending the reversal. Regression: `tests/test_customer_paper_ledger.py::test_reversing_sell_cannot_restore_exposure_above_shared_risk_budget`. |
| 3 | A committed paper fill can be reported as failed | Qodo marked resolved | Cleanup failures do not replace committed fill outcomes. |
| 4 | Slow quote fetches stall other customers fills | Qodo marked resolved | Customer-scoped lock/quote-fetch sequencing is covered by lifecycle regressions. |
| 5 | Broader mark fetches can block buys | Qodo marked resolved | Mark preflight is scoped and validated. |
| 6 | Valid position increases are cut short | Qodo marked resolved | Reducing quantity is preserved and only an increase is capped. |
| 7 | Unpriced holdings do not block new buys | Qodo marked resolved | Unclassified exposure is conservatively included; missing marks fail closed. |
| 8 | A failed lock wait is logged as a lock-release failure | Implemented in current main | The BUY path tracks attempted and acquired lock states separately. It invalidates a connection with uncertain acquisition outcome rather than unlocking a lock it may not hold. Regression: `tests/test_customer_paper_ledger.py::test_customer_paper_fill_fetches_marks_before_write_and_legacy_locks_after_order_bar`, including the `fail_lock_wait=True` case. |
| 9 | Fills can deadlock while old and new workers run side by side | Qodo marked resolved | Legacy correlation locks use compatible ordering. |
| 10 | A behavioural ordering test became a source-text check | Qodo marked resolved | Behavioral lock-order regression retained. |
| 11 | Old and new servers can overspend a buy cap together | Qodo marked resolved | Both worker paths participate in compatible shared-budget locking. |

## PR401 — evidence-backed per-bar research execution

Original review: [#401](https://github.com/Papajona/Atlas-Bot/pull/401), Qodo comment `6866337821`. The snapshot contained 18 findings. Findings 1–6 and 18 were historically active; Qodo marked findings 7–17 resolved.

| # | Historical finding | Maintainer disposition | Evidence |
|---:|---|---|---|
| 1 | Session-priced fallbacks look unverified | Fixed in this follow-up | `session_cost_model_complete` now reflects the schedule actually applied, including per-bar rejection fallback. Benchmark paths retain the verified session schedule for a fold-local fallback. Regression: `test_session_cost_completeness_reflects_the_schedule_used_after_per_bar_rejection`. |
| 2 | Fractional bar indices price the wrong bar | Fixed in current main | Current `execution_cost_for_event()` rejects bools and fractional/precision-losing indices before selecting signed evidence rows. Regression: `tests/test_research_execution_costs.py::test_execution_event_rejects_boolean_or_fractional_indices_and_quantities`, plus integral-numeric acceptance coverage. |
| 3 | Position closes get different latency costs | Fixed in this follow-up | A normal transition from a non-flat position to flat uses the EXIT event namespace; initial entries and adjustments remain distinct. Regression: `test_execution_simulator_prices_ordinary_transition_to_flat_as_exit`. |
| 4 | Benchmark report vanishes when a control trade is too large | Fixed in this follow-up | If a candidate, buy-and-hold, or random-control event exceeds the signed per-bar limit, the affected fold is downgraded to the separately verified session schedule (or static diagnostic costs) and **all** benchmark legs are recomputed. The report keeps candidate, buy-and-hold and random-control diagnostics, but execution-cost completeness and the promotion gate remain false. Regression: `test_benchmark_control_cost_rejection_falls_back_and_preserves_diagnostics`. |
| 5 | Signed session costs no longer bound to session model | Implemented in current main | `research_execution_model_fingerprint()` includes `SESSION_COST_MODEL_VERSION`; regression: `test_research_execution_fingerprint_binds_session_cost_algorithm`. The runbook says signatures that omit this identifier must be re-signed and re-validated. |
| 6 | Valid signed evidence is rejected | Implemented in current main | Fee-schedule and order-book source types are normalized using `.strip().lower()`; regression: `test_signed_source_types_are_normalized_case_insensitively`. |
| 7 | Cloud Run template stops the API at startup | Qodo marked resolved | Original review disposition. |
| 8 | Runbook puts fee fields where the code ignores them | Qodo marked resolved | Original review disposition. |
| 9 | Altered bars retain signed cost status | Qodo marked resolved | Source hash is recomputed against current OHLCV values. |
| 10 | Router validation silently drops the configured cost | Qodo marked resolved | Shared event-cost kernel is wired into router returns. |
| 11 | Adaptive router validation slows sharply without evidence | Qodo marked resolved | Original review disposition. |
| 12 | Benchmark controls use cheaper cost rules | Qodo marked resolved | Benchmark controls use the verified cost context. |
| 13 | Adaptive router backtest becomes very slow | Qodo marked resolved | Original review disposition. |
| 14 | Policy reversals get one fill cost | Qodo marked resolved | Reversals are separately priced as close and open events. |
| 15 | Variant metrics come from two differently priced streams | Qodo marked resolved | Original review disposition. |
| 16 | One report gives conflicting cost status | Qodo marked resolved | Execution status fields are propagated into the report. |
| 17 | Old latency interpolation helper left behind | Qodo marked resolved | Duplicate helper was removed in favor of the shared implementation. |
| 18 | Benchmark numbers vanish when a fill is too large | Implemented in current main | When the candidate stream falls back after a participation rejection, `_benchmark_execution_inputs()` removes the rejected per-bar context from benchmark controls and retains the session schedule. Regression: `test_benchmark_cost_context_is_disabled_after_candidate_event_rejection`. |

## Maintainer result

- PR396 findings 1, 3 and 4 are fixed by merged PR398; the other three were already marked resolved by Qodo.
- All PR397 and PR398 Qodo findings were marked resolved in their original reports.
- PR399's active item is superseded by merged PR405, which Qodo reviewed with zero material issues.
- PR400's two historical active findings are implemented in current main and have dedicated regressions.
- PR401 findings 2, 5, 6 and 18 are implemented in current main. Findings 1, 3 and 4 are addressed by this follow-up branch and must pass the exact-commit CI/mandatory PostgreSQL/Production Core checks before being called merged.
- A Qodo historical “active” badge is not an Atlas application-progress status. It does not block verified code or control release. It remains only a record of what the bot reported against an earlier PR head.

**Live trading, live payouts, withdrawals and customer-fund movement remain governed by their existing independent safety and production-approval gates.**
