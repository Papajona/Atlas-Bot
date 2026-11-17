# Evaluating rejected Atlas signals

This report is read-only and research-only. It consumes persisted `ADAPTIVE_DECISION_OUTCOME_LABEL` audit events; it does not place orders, change decision gates, train models, or promote strategies.

## Run

After outcome labels have been generated and reviewed:

```bash
python scripts/evaluate_rejected_signals.py --holdout-fraction 0.30 --min-group-samples 30
```

By default the report loads the complete audit-label history within the shared 200,000-record / 256 MiB safety budget. It does not silently take the newest 10,000 rows. To inspect one methodology only, select it explicitly; an optional limit is applied after version filtering:

```bash
python scripts/evaluate_rejected_signals.py --label-version decision-outcome-v1 --limit 10000
python scripts/evaluate_rejected_signals.py --label-version decision-outcome-v2
```

The report's `history_selection` and `coverage` fields state which versions, counts and market-timestamp date ranges were loaded. A `--limit` without `--label-version` is rejected so newer rows from one methodology cannot silently displace another.

To save the full-history JSON report as an artifact:

```bash
python scripts/evaluate_rejected_signals.py --holdout-fraction 0.30 --min-group-samples 30 --output reports/rejected-signal-evaluation.json
```

The `rejected-signal-evaluation-v4` report includes:
- Counts and coverage by decision class, mode, label version, duplicate audit rows and candidate-side availability.
- Candidate-side hypothetical net-return summaries for TRADE and NO_TRADE decisions, grouped separately by asset, symbol, exchange, timeframe, mode, strategy, regime, label version, cost source/assumptions and horizon.
- Repeated audit rows for the same decision and label version are counted once; the latest persisted row is retained.
- NO_TRADE summaries split by recorded stage and reason code.
- A single shared UTC chronological holdout cutoff, derived from the union of eligible decision timestamps across all groups. The same calendar boundary is applied to every market/strategy subgroup; a subgroup cannot select its own more favorable date.
- Groups with no pre-cutoff training observations after outcome-overlap purging are marked `INSUFFICIENT_TRAINING_BASELINE` and excluded from one-time holdout release.
- Training-side labels whose outcome candle closes at or after the shared cutoff are purged to avoid overlapping outcome windows.
- Sample-size flags. Small groups remain visible but are not marked sample-sufficient; groups with no observations after the shared cutoff cannot contribute holdout metrics.

## Interpretation rules

- The return metric is a hypothetical candidate-side return based on the recorded market outcome and configured taker/slippage/carry assumptions. It is not actual fill-level P&L.
- A positive hypothetical return after a NO_TRADE decision does not prove the rejection was wrong; the risk, data-quality or safety gate may have been correct.
- The holdout is descriptive, not a statistical significance test. No p-values, strategy promotion or automatic learning changes are produced.
- The one-time evaluator uses an immutable global registry scope. A consumed holdout is not reopened by changing parameters, adding groups or changing the report's sample composition. Release eligibility requires a non-empty post-purge training baseline and adequate TRADE/NO_TRADE holdout samples.
- Review dataset/source hashes and label version before comparing reports. Do not tune on the holdout and then present that same holdout as untouched evidence.
- If there are no persisted labels, the report will show zero valid labels; it must not infer outcomes from unlabelled decisions.
