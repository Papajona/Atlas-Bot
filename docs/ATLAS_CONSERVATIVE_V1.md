# Atlas Conservative v1 implementation and evidence gates

## Status

Implemented as a frozen risk profile and research-only order policy. The central order path rejects live execution for `atlas_conservative_v1`. This is not a claim of profitability or live readiness.

## Immutable profile

- Risk per trade: 0.25% of current account equity.
- Daily loss stop: 1% from start-of-day equity.
- Peak-to-trough drawdown stop: 5%.
- Maximum open positions: 2.
- Maximum notional leverage: 1x equity.
- Minimum reward/risk: 2:1 before actual fill uncertainty; execution tests must include costs.
- Maximum spread: 30 bps; this is a conservative initial test ceiling, not a calibrated universal value.

The frozen dataclass is in `app/conservative_strategy.py`. The execution path enforces order-level metadata/risk constraints and rejects live use. The existing central risk gate applies the tighter daily-loss, drawdown, leverage and position-count limits when the strategy ID is exactly `atlas_conservative_v1`.

## Instrument metadata

Sizing fails closed unless the server-side strategy supplies validated instrument metadata: contract multiplier, quote-to-account-currency conversion, amount step, min/max amount and price tick. Client-provided metadata must not be trusted for live execution. No broker adapter is approved for this profile's live execution.

## Regime annualization

`app/strategy_router.py` now estimates periods per year from median observed bar spacing and an explicit active-hours calendar:
- crypto: 24 hours/day × 365.25 days/year;
- forex: 24 hours/day × 252 days/year;
- commodities: 23 hours/day × 252 days/year;
- equities/indices: 6.5 hours/day × 252 days/year.

These are approximations. They must be validated for each actual feed/timeframe and its gaps/session conventions. Do not interpret them as an exchange-specific calendar.

## Execution-aware research simulator

`execution_aware_backtest` enters on the next bar open, applies fee/slippage assumptions, treats simultaneous stop/target hits pessimistically (stop first), and models adverse stop gaps. It remains a simplified OHLC simulator: it does not model partial fills, order-book depth, broker latency, financing, dividends, funding rates, margin calls, or all instrument-specific order rules. It is not sufficient alone for promotion.

## Required tests and remaining evidence

- Unit tests for immutable policy, sizing, missing metadata, leverage/risk bounds, spread checks, next-open entries, stop/target ambiguity and annualization cadence are in `tests/test_conservative_strategy.py` and `tests/test_conservative_strategy_router.py`.
- CI must pass on the final commit before code-level verification is claimed.
- Add provider-specific tests that obtain metadata from trusted adapter calls, not request payloads.
- Verify each strategy family and each adapter through the central boundary.
- Run identified historical data through chronological walk-forward tests with fees/slippage and multiple-testing adjustment.
- Run per-market practice/paper validation for 28 consecutive days, followed by the repository's 90-day final evidence requirement.
- Complete the separate money-path concurrency, withdrawal recovery, route-module, signed Android and staging recovery gates.

Until these evidence gates pass, this profile is research/paper-only. Live trading remains disabled.
