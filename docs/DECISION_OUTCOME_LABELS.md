# Research-only adaptive decision outcome labels

## Purpose and safety boundary

The labeler measures what the market did after a recorded Atlas decision. It is research-only: it does not rewrite the original decision snapshot, create or cancel orders, train a model, change a champion, or promote a strategy.

Labels are stored as separate `ADAPTIVE_DECISION_OUTCOME_LABEL` events in the existing encrypted, hash-chained audit log. No database migration is introduced.

## Dry-run first

From the repository root:

```bash
python scripts/label_adaptive_decision_outcomes.py --limit 100 --horizons 1,3,6 --days 365
```

The default is dry-run. It prints a JSON summary and up to ten sample labels without writing them.

After reviewing the preview and its data provenance, persist research labels explicitly:

```bash
python scripts/label_adaptive_decision_outcomes.py --limit 100 --horizons 1,3,6 --days 365 --persist
```

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

The label version is `decision-outcome-v1`. A methodology change must bump that version. Review `outcome_window_sha256`, `source_dataset_sha256`, `market_timestamp`, the selected horizons, and the recorded costs before comparing labels across runs.
