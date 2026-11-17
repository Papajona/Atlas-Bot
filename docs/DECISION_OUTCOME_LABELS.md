# Research-only adaptive decision outcome labels

## Purpose and safety boundary

The labeler measures what the market did after a recorded Atlas decision. It is research-only: it does not rewrite the original decision snapshot, create or cancel orders, train a model, change a champion, or promote a strategy.

Labels are stored as separate `ADAPTIVE_DECISION_OUTCOME_LABEL` events in the existing encrypted, hash-chained audit log. No database migration is introduced.

## Dry-run first

From the repository root:

```bash
python scripts/label_adaptive_decision_outcomes.py --limit 100 --horizons 1,3,6 --days 365
```

The default is dry-run. It prints a JSON summary and up to ten sample labels without writing them. The summary includes `pending_decisions_for_version` (the full number of decisions not yet labelled under the active version) and `label_history_capacity` (current rows, remaining slots and the maximum safe batch).

After reviewing the preview, data provenance and capacity, persist research labels explicitly in batches:

```bash
python scripts/label_adaptive_decision_outcomes.py --limit 10000 --horizons 1,3,6 --days 365 --persist
```

Repeat the same command until `pending_decisions_for_version` reaches zero. Each successful batch is appended as new audit events; the original v1 rows are never overwritten. Records that cannot yet be labelled remain pending and must not be counted as completed. The writer refuses a batch before appending anything when the total label-audit history would exceed 200,000 records, a bounded budget sized for up to 100,000 v1 rows plus 100,000 v2 relabel rows.

Use the deployment's approved database environment. The command does not need exchange trading permissions; the crypto adapter fetches public OHLCV. The OANDA adapter is the existing practice/demo-only fetcher.

## Label semantics

- The decision's `market_timestamp` is interpreted as a candle-open timestamp, and the exact matching candle is required. The code never substitutes a nearest candle.
- The baseline is the close of that exact decision candle, after it is complete.
- Horizons `1,3,6` mean the next 1, 3, and 6 observed completed candles, not a guessed wall-clock interval. Each outcome records actual elapsed seconds and the largest gap between observed candles.
- Forward return is reported in basis points. Long and short gross returns and configured-cost-adjusted hypothetical returns are both retained. If the original record contains no unambiguous buy/sell candidate side, market outcomes are still labelled but candidate-side return is null.
- Per-side costs come from Atlas's configured asset profile. Crypto currently uses the profile's 5.5 bps taker plus 2.0 bps slippage and 0.125 bps carry per observed bar; forex uses 0.5 plus 0.5 bps and 0.0 carry. Hypothetical net returns subtract two sides of taker-plus-slippage and the horizon's configured carry. These are configured assumptions, not proof of actual execution costs.
- Labels include the source dataset provenance hash and a separate SHA-256 hash of the exact candles used for the outcome. The label identity does not change if candles strictly after the longest labelled horizon change.
- The original decision snapshot hash is retained as the source link. Labels do not flow back into the original record.

## Refusal conditions

The labeler skips decisions when the exact decision candle is absent, future candles are not yet complete, required market metadata is missing, source provenance does not match, or no verified adapter/cost profile exists. Commodity labels remain unsupported until Atlas has a verified commodity data adapter. Forex sources must be explicitly recorded as `oanda` or `yfinance`; other source names are not silently remapped.

A rejected signal followed by a favourable price move is not automatically a bad rejection. Risk, data-quality, and safety gates must be evaluated separately from hypothetical directional returns.

## Reproducibility

The active label version is `decision-outcome-v2`. Version v2 uses next-open executable candidate-side returns; v1 labels carry close-to-close fills and are not regenerated in place. The labeler skips a decision only when a valid label for the active version already exists, so a v1-labelled decision remains eligible for v2. Research uses executable returns only for v2 and groups v1 and v2 separately. Review `outcome_window_sha256`, `source_dataset_sha256`, `market_timestamp`, the selected horizons, and the recorded costs before comparing labels across runs.
