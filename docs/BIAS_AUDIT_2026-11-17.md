# Bias audit and paper-testing patches — 17 November 2026

Baseline: protected `main` before this change. Scope: signal/feature timing, labels, selection/overfitting controls, execution optimism and paper-test evaluation bias. This is a targeted code audit, not a line-by-line audit of the full repository.

This document records code changes and tests. It is not exact-SHA CI evidence and does not supersede `deploy/SIGNOFF_MATRIX.md`. No performance claim is authorised.

## Fixed in this patch

| ID | Severity | Bias | Finding | Fix |
|---|---|---|---|---|
| BA-01 | High for meta-filter evidence | Executable-timing (free-gap) optimism | `build_meta_dataset` labelled `close[t+h]/close[t]`, granting a fill at the signal bar close. | Labels now use `open[t+h+1]/open[t+1]`; source-bar purge/embargo and label-end reporting include the next-open entry offset. `open` is required and the result reports `label_timing`. |
| BA-02 | Medium | Rejected-signal outcome optimism | Decision outcome research measured candidate returns from the decision candle close. | Labels retain legacy close-to-close fields but add next-open entry and executable candidate-side returns. Research prefers executable returns and fails closed for unavailable executable fills. Legacy fallback applies only when `entry_timing` is absent. |

Regression tests: `tests/test_bias_audit_2026_11_17.py`.

## Timing conventions

- Meta-labels use next-open entry to the open after the configured holding horizon, matching the portfolio backtester return convention.
- Decision-outcome labels use next-open entry to the close of the requested subsequent observed candle horizon. The legacy close-to-close fields remain for audit continuity, but new research consumes the executable candidate-side return.
- A new-format label marked `NEXT_OPEN_UNAVAILABLE`, or marked `NEXT_OPEN` without an executable return, contributes no return to research.

## Open: evidence, not code

1. Original OANDA dataset identity and financing/carry evidence remain unresolved; zero carry must not be interpreted as evidence of zero financing.
2. Paper fills remain incomplete without queue/depth modelling. OHLCV-only paper results are diagnostic, not validation-grade.
3. Persisted legacy outcome labels (`decision-outcome-v1`) lack executable fields. The label version is bumped to `decision-outcome-v2`, so previously labelled decisions are re-labelled under v2 and research groups the two methodologies separately. Re-run `scripts/label_adaptive_decision_outcomes.py` in dry-run mode first; verify the affected record count and output before any write.

## Paper-test protocol

- Freeze the strategy config hash, symbols, timeframe, test dates, minimum trade count and pass/fail metrics before day 1.
- Do not retune during the window. A configuration change starts a new trial and is counted.
- Report every symbol and strategy run, including losses and halted runs.
- Report net returns after configured costs and a stressed-cost run; evaluate rejected signals on executable returns.
- Treat the window as a small-sample falsification test, not proof of predictive skill.

## Verification status

No local test counts or CI results are asserted by this document. The pull-request checks on the exact proposed commit are the required verification evidence.