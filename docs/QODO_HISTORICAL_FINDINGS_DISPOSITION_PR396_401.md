# Maintainer disposition of historical Qodo findings — PRs 396–401

**Repository:** `Papajona/Atlas-Bot`  
**Current-main baseline reviewed:** `fd5ba96967fc4e7f94aa9ff5aedb2e567e48b3cc`  
**Disposition owner:** Atlas-Bot maintainers.  
**This file records code status; it does not edit or replace Qodo's historical comments.**

## Authority

Qodo is advisory code review only. It has no authority to merge, deploy, alter trading/payout controls, decide application completion, or certify production readiness. Its findings are not an application release gate. Merges and releases remain under maintainer control and are determined by exact-commit CI, mandatory PostgreSQL verification, Production Core Verification, security checks, and separately approved production evidence.

Qodo has stated in the PR discussions that it cannot re-review a closed PR against a different `main` commit. The available GitHub integration cannot rewrite Qodo-authored comments or mutate Qodo's external finding catalog. Old “active” badges are therefore historical snapshots, not proof that the current code still has the defect. This maintainer ledger is the code disposition. It does not pretend to have changed Qodo's original labels.

## Summary

| PR | Original Qodo comment | Findings | Historical active labels | Maintainer disposition |
|---|---|---:|---:|---|
| [#396](https://github.com/Papajona/Atlas-Bot/pull/396) | `6858617806` | 6 | 3 | Three active findings fixed by merged #398; remaining three Qodo-marked resolved. |
| [#397](https://github.com/Papajona/Atlas-Bot/pull/397) | `6859652257` | 8 | 0 | All eight Qodo-marked resolved. |
| [#398](https://github.com/Papajona/Atlas-Bot/pull/398) | `6860270003` | 2 | 0 | Both Qodo-marked resolved. |
| [#399](https://github.com/Papajona/Atlas-Bot/pull/399) | `6862532377` | 1 | 1 | Fixed by merged #405; Qodo's #405 review reported zero material issues. |
| [#400](https://github.com/Papajona/Atlas-Bot/pull/400) | `6862978439` | 11 | 2 | Both historical active findings implemented in current main and regression-tested. |
| [#401](https://github.com/Papajona/Atlas-Bot/pull/401) | `6866337821` | 18 | 7 | #2, #5, #6, #18 implemented in current main; #1, #3, #4 fixed in this follow-up; #7–17 already Qodo-marked resolved. |

## PR396 — withdrawal replay integrity

Original Qodo review [#396](https://github.com/Papajona/Atlas-Bot/pull/396), comment `6858617806`.

| # | Historical finding | Maintainer disposition | Evidence |
|---:|---|---|---|
| 1 | Incident outages strand customer reserves | Fixed in merged #398 | Caller-session SAVEPOINT; confirmed terminal withdrawal state and HIGH incident commit together. Test: `tests/test_withdrawal_ledger_repair_db.py::test_replay_incident_write_failure_rolls_back_terminal_withdrawal_too`. |
| 2 | Ledger conflicts strand withdrawal reserves | Qodo marked resolved | Authorized ledger-repair flow. |
| 3 | Concurrent mismatch replays crash after the incident is saved | Fixed in merged #398 | SAVEPOINT isolates duplicate incident-key races without rolling back/expiring caller ORM state. Test: `test_duplicate_replay_incident_race_preserves_async_session_objects`. |
| 4 | Repair cannot release reserves for a real replay conflict | Fixed in merged #398 | Repair verifies the corrected original journal and customer ledger; it does not repeat the failed idempotent posting. Tests: `test_repair_refuses_a_real_unfixed_replay_journal`, `test_rejected_terminal_withdrawal_has_authorized_ledger_repair`, `test_repair_rejects_valid_journal_when_customer_reserve_is_still_locked`. |
| 5 | Orphaned releases accept changed amounts | Qodo marked resolved | Missing source-withdrawal identity fails closed. |
| 6 | Mismatch incidents are lost on SQLite | Qodo marked resolved | Incident persistence uses the caller's transaction. |

PR398 merge commit: `88411c1f86383931b80a9f2a377f1a6a8c610e5c`.

## PR397 — shared execution-cost kernel

Original Qodo review [#397](https://github.com/Papajona/Atlas-Bot/pull/397), comment `6859652257`. All eight findings were Qodo-marked resolved. Its later response `6861686189` explicitly reconfirmed findings 7 and 8 on exact PR head `fbeba7a7739d020af47df3642ebfac02c5d31e36`.

1. Locked installs still ship the vulnerable OpenTelemetry API — resolved.
2. Test wipes other platform positions in the shared DB — resolved.
3. Stop-trigger test now passes vacuously — resolved.
4. Invalid cost attempts get misreported — resolved.
5. Missing closes abort label construction — resolved.
6. Model training slows on long histories — resolved.
7. Invalid-cost test disables the position cap instead of isolating state — resolved and explicitly reconfirmed by Qodo.
8. Cost-context test skips the real invalid-input path — resolved and explicitly reconfirmed by Qodo.

## PR398 — withdrawal repair follow-up

Original Qodo review [#398](https://github.com/Papajona/Atlas-Bot/pull/398), comment `6860270003`. Both findings were Qodo-marked resolved.

1. Customers' repaired funds remain locked — resolved; repair verifies journal-derived ledger buckets before wallet synchronization.
2. Valid repairs can fail on SQLite rounding — resolved; SQLite ledger reconciliation accumulates journal values using exact decimals.

## PR399 — training-label replay gate

Original Qodo review [#399](https://github.com/Papajona/Atlas-Bot/pull/399), comment `6862532377`.

1. Replay gate no longer lists the label fix as open work — fixed by merged [#405](https://github.com/Papajona/Atlas-Bot/pull/405). `train_model()` calls `_encode_triple_barrier_labels()`, validating finite labels in `{-1, 0, 1}` and mapping them to classifier classes `{0, 1, 2}`. The OANDA replay guide requires separate training-label and sparse/reordered prediction-probability tests.

Tests:
- `tests/test_core.py::test_triple_barrier_training_labels_encode_as_classifier_classes`
- `tests/test_core.py::test_predict_latest_maps_sparse_reordered_class_probabilities`

PR405 merge commit: `7dedd403f8c7e01dc72364061b1c5a81033cbb48`. Qodo's PR405 review comment `6875865841` reported 0 bugs, 0 rule violations and 0 requirement gaps.

## PR400 — customer-paper risk budgets and lock handling

Original Qodo review [#400](https://github.com/Papajona/Atlas-Bot/pull/400), comment `6862978439`. Nine findings were Qodo-marked resolved; #2 and #8 were historically active.

| # | Historical finding | Maintainer disposition | Evidence |
|---:|---|---|---|
| 1 | A regression test fails after mark loading changes | Qodo marked resolved | Original review disposition. |
| 2 | Reversals can exceed shared buy budgets | Implemented in current main | SELL reversals acquire the customer risk-budget lock, require fresh verified marks, value projected shared exposure at the higher of mark and booked entry, and reject cap breaches before posting. PostgreSQL test: `tests/test_customer_paper_ledger.py::test_reversing_sell_cannot_restore_exposure_above_shared_risk_budget`. |
| 3 | A committed paper fill can be reported as failed | Qodo marked resolved | Cleanup errors do not replace committed fill outcomes. |
| 4 | Slow quote fetches stall other customers fills | Qodo marked resolved | Customer-scoped lock/quote sequencing is covered by lifecycle regressions. |
| 5 | Broader mark fetches can block buys | Qodo marked resolved | Mark preflight is scoped and verified. |
| 6 | Valid position increases are cut short | Qodo marked resolved | Reductions are preserved; only increases are capped. |
| 7 | Unpriced holdings do not block new buys | Qodo marked resolved | Unclassified exposure is counted conservatively; missing marks fail closed. |
| 8 | A failed lock wait is logged as a lock-release failure | Implemented in current main | Attempted and acquired lock states are separate. Uncertain acquisition invalidates the connection without attempting an unlock. Regression: `tests/test_customer_paper_ledger.py::test_customer_paper_fill_fetches_marks_before_write_and_legacy_locks_after_order_bar`, including `fail_lock_wait=True`. |
| 9 | Fills can deadlock while old and new workers run side by side | Qodo marked resolved | Legacy group locks use compatible ordering. |
| 10 | A behavioural ordering test became a source-text check | Qodo marked resolved | Behavioral lock-order regression retained. |
| 11 | Old and new servers can overspend a buy cap together | Qodo marked resolved | Both worker paths participate in shared-budget locking. |

## PR401 — evidence-backed per-bar research execution

Original Qodo review [#401](https://github.com/Papajona/Atlas-Bot/pull/401), comment `6866337821`. The snapshot contained 18 findings. #1–6 and #18 were historically active; #7–17 were Qodo-marked resolved.

| # | Historical finding | Maintainer disposition | Evidence |
|---:|---|---|---|
| 1 | Session-priced fallbacks look unverified | Fixed in this follow-up | `session_cost_model_complete` reflects whether the candidate stream actually used the separately verified session schedule, including per-bar event-rejection fallback. Benchmark inputs retain that schedule for fold-local fallback. Test: `test_session_cost_completeness_reflects_the_schedule_used_after_per_bar_rejection`. |
| 2 | Fractional bar indices price the wrong bar | Fixed in current main | `execution_cost_for_event()` rejects bools and fractional/precision-losing indices before selecting a signed evidence row. Test: `tests/test_research_execution_costs.py::test_execution_event_rejects_boolean_or_fractional_indices_and_quantities`; integral numeric indices have separate acceptance coverage. |
| 3 | Position closes get different latency costs | Fixed in this follow-up | Ordinary transition from an open position to flat uses the EXIT event namespace; initial entries and adjustments remain distinct. Test: `tests/test_execution_aware_research.py::test_ordinary_signal_close_uses_exit_event_namespace`. |
| 4 | Benchmark report vanishes when a control trade is too large | Fixed in this follow-up | If a candidate, buy-and-hold, or random-control event exceeds the signed per-bar limit, only the affected fold is downgraded to the verified session schedule (or static diagnostics), and all benchmark legs are recomputed. Diagnostics remain available while model completeness and promotion gate stay false. Test: `test_benchmark_control_cost_rejection_falls_back_and_preserves_diagnostics`. |
| 5 | Signed session costs no longer bound to session model | Implemented in current main | Fingerprint contains `SESSION_COST_MODEL_VERSION`; test: `test_research_execution_fingerprint_binds_session_cost_algorithm`. Runbook requires signatures omitting this identifier to be re-signed and revalidated. |
| 6 | Valid signed evidence is rejected | Implemented in current main | Fee-schedule/order-book source types use `.strip().lower()`; test: `test_signed_source_types_are_normalized_case_insensitively`. |
| 7 | Cloud Run template stops the API at startup | Qodo marked resolved | Original review disposition. |
| 8 | Runbook puts fee fields where the code ignores them | Qodo marked resolved | Original review disposition. |
| 9 | Altered bars retain signed cost status | Qodo marked resolved | Current OHLCV hash is recomputed. |
| 10 | Router validation silently drops the configured cost | Qodo marked resolved | Shared event-cost path is used. |
| 11 | Adaptive router validation slows sharply without evidence | Qodo marked resolved | Original review disposition. |
| 12 | Benchmark controls use cheaper cost rules | Qodo marked resolved | Benchmark controls use the verified cost context. |
| 13 | Adaptive router backtest becomes very slow | Qodo marked resolved | Original review disposition. |
| 14 | Policy reversals get one fill cost | Qodo marked resolved | Reversal close and open events are separately priced. |
| 15 | Variant metrics come from two differently priced streams | Qodo marked resolved | Original review disposition. |
| 16 | One report gives conflicting cost status | Qodo marked resolved | Execution status propagates through report fields. |
| 17 | Old latency interpolation helper left behind | Qodo marked resolved | Duplicate helper removed. |
| 18 | Benchmark numbers vanish when a fill is too large | Implemented in current main | Candidate fallback removes the rejected per-bar context from benchmark inputs and retains the verified session schedule. Test: `test_benchmark_cost_context_is_disabled_after_candidate_event_rejection`. |

## PR406 — Qodo follow-up review (2026-11-14)

The latest Qodo review comment `6876549905` is tied to the exact PR406 head
`e540ad4660f31b88b22c607747b0dd08d21ad498`. Its current report contains ten
findings, and all ten are marked resolved. The rows below record that latest
report once. The session-status finding IDs
`2058abdb-cc66-40be-b462-3734faeec4c9` and
`967e5bef-327c-4517-85f1-0d2b5dc8a2b6` are each listed once.

| Qodo finding | Disposition | Code/test evidence |
|---|---|---|
| Session-priced returns appear unverified | Qodo-marked resolved at `7e4aecc` | The status derives from the selected pricing source: if no precomputed per-bar transaction-cost series is supplied, the session schedule is the selected candidate pricing branch even when an explicit audit flag is false. When a precomputed series is supplied, its audit flag still distinguishes session-derived from signed per-bar pricing. Regression: `tests/test_research_bias_audit_fixes.py::test_fixed_signal_oos_reports_session_schedule_when_selected_without_precomputed_series`. |
| Session-only runs list every fold as a fallback | Qodo-marked resolved at `fba86bb` | The fallback-fold set is populated only from actual `fold_N` rejection reasons or the explicit upstream candidate-fallback marker. Regressions: `test_session_only_benchmark_is_not_reported_as_a_fallback` and `test_fixed_signal_oos_marks_upstream_event_rejection_as_benchmark_fallback`. |
| Benchmark fallback fails for generator inputs | Qodo-marked resolved at `304dd0a` | The benchmark materializes reusable fold arrays and reuses them for the single retry rather than re-reading consumed generators. The control-rejection regression covers generator-backed signal, candidate-return and ATR inputs. |
| `2058abdb-cc66-40be-b462-3734faeec4c9` — Session-priced candidates appear unverified | Qodo-marked resolved at `2ff685a` | Each ensemble/component OOS report is paired with its matching simulator audit. `_set_session_cost_report_status()` updates top-level and nested `cost_analysis` from that audit. Regression: `tests/test_research_execution_costs.py::test_session_cost_report_status_uses_the_matching_simulator_audit`. |
| `967e5bef-327c-4517-85f1-0d2b5dc8a2b6` — Verified runs show conflicting cost flags | Qodo-marked resolved at `2ff685a` | `fixed_signal_walk_forward_oos()` receives the candidate's explicit audit flag when a precomputed series is supplied, so a schedule retained only for benchmark fallback is not reported as used by a verified per-bar candidate. Regression: `tests/test_research_bias_audit_fixes.py::test_fixed_signal_oos_session_cost_flags_match_the_candidate_pricing_source`. |
| Ledger gives one Qodo comment two meanings | Qodo-marked resolved in the updated review | The ledger now records comment `6876549905` once, gives it one reviewed head and one finding table, and does not duplicate the two session-status IDs. |
| Disposition ledger contradicts itself on open findings | Qodo-marked resolved at `2142c1c` | The PR406 disposition summary and its finding rows use one consistent count and status. |
| Partial fallback reports buy-and-hold as session-priced | Qodo-marked resolved at `bc4b082` | Buy-and-hold completeness and the published session schedule require every fold to have used session pricing. The report separately lists actual fallback fold indices. Regression: `tests/test_research_execution_costs.py::test_benchmark_fallback_downgrades_all_rejected_folds_in_one_retry`. |
| Each rejected fold reruns the whole benchmark | Qodo-marked resolved at `304dd0a` | Candidate, buy-and-hold and random-control rejections are collected across folds before one aligned retry, rather than recursively rerunning the whole benchmark for each rejected fold. Regression: `test_benchmark_fallback_downgrades_all_rejected_folds_in_one_retry`. |
| Session-only runs are reported as fallbacks | Qodo-marked resolved at `304dd0a` | `execution_cost_model_fallback_used` is true only when an execution-cost rejection exists. Normal session-only pricing reports `INCOMPLETE_SESSION_BENCHMARK_COSTS`. Regressions: `test_session_only_benchmark_is_not_reported_as_a_fallback` and `test_fixed_signal_oos_marks_upstream_event_rejection_as_benchmark_fallback`. |

The separate Qodo fixer response (`6876551111`) is a fixer-scope message, not a
review disposition. Qodo remains advisory; source changes and exact-head CI are
the application verification gates.


## PR407 — readiness and telemetry follow-up

Qodo's latest review comment on [PR407](https://github.com/Papajona/Atlas-Bot/pull/407) reports four findings and marks all four resolved:

| Qodo finding | Qodo disposition | Code/test evidence |
|---|---|---|
| Readiness TTL and fail-closed settings were undeclared on `Settings` | Resolved against `52855ab` | `app/config.py` declares `readyz_cache_seconds=5.0` and `readyz_fail_when_not_ready=True`; regression checks defaults and the finite/non-negative validator. |
| The defaults-test parametrization broke test collection | Resolved against `57b5ebb` | The defaults test is undecorated; the invalid-TTL parametrization is attached to `test_readyz_cache_ttl_must_be_finite_and_non_negative`. |
| The readiness settings test failed in CI | Qodo marked resolved | The latest PR407 Qodo comment records the resolution; exact-head Atlas CI is still required for independent verification. |
| SQL template change hid the query from Bandit | Qodo marked resolved | The latest PR407 Qodo comment records the resolution; Production Core and security verification remain required. |

PR408 is Qodo's automated cherry-pick/fixer PR for the readiness fields already added to PR407. It is closed and unmerged; it is not a separate Atlas code fix or a reason to merge the source PR.

## Maintainer result

- PR396's historical active findings are fixed by merged PR398.
- All PR397 and PR398 Qodo findings were marked resolved in their published reviews.
- PR399's only active item is fixed by merged PR405, which Qodo reviewed with zero material findings.
- PR400's two historical active findings are implemented in current main with behavioral/PostgreSQL regressions.
- PR401 findings #2, #5 and #6 are implemented in current main; #1 and #4 are fixed in the PR406 follow-up; #3 now has an explicit EXIT-namespace behavioral regression on PR406 head `4393fa51`; #18 is covered by the same batched benchmark-fallback implementation.
- PR406's ten latest Qodo findings were marked resolved in its published review on exact head `e540ad4660f31b88b22c607747b0dd08d21ad498`. PR407's startup and test-collection findings were marked resolved in its follow-up review; PR407 remains subject to exact-head CI and PostgreSQL verification.
- PR402/#403 did not receive Qodo review comments because the bot's review quota was exhausted; their relevant risk-lock/reversal code and regressions were manually checked against current main.
- PR408 is a Qodo auto-fix cherry-pick PR for the readiness settings already implemented on #407; it contains no separate Qodo review findings.
- Historical Qodo labels do not determine Atlas application progress, block a verified merge, or control release.

**Live trading, live payouts, withdrawals, and customer-fund movement remain governed by independent safety and production-approval gates.**
