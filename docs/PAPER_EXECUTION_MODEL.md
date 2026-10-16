# Platform paper-execution cost and fill model

## Scope

This is a deterministic, platform-only paper simulator. It submits no broker orders and does not change the live execution path. Customer paper execution remains blocked until a customer-isolated simulation ledger is implemented and tested.

A paper order is accepted as `OPEN` with zero filled quantity. It cannot fill on its submission bar. Only a later, new completed bar can advance it. The simulator accepts base-asset bar volume as liquidity only when the bar context has provenance `ccxt_ohlcv`, asset class `crypto`, and unit `base_asset`. OANDA/OTC candle counts and Yahoo Finance volume are not accepted as executable base-asset liquidity; such orders wait for verified data and ultimately expire under the configured time-to-live (TTL).

## Fill lifecycle

On a later bar, the simulator applies the configured minimum bar latency, a deterministic order/bar hash to approximate queue misses and rejection, and a maximum aggregate participation budget for all matching orders on the same exchange/symbol/timeframe/bar. Partial fills preserve remaining quantity; a repeated bar cannot be consumed twice; orders that cannot complete within TTL have their remaining quantity canceled. The aggregate cap is tested under hundreds of synthetic orders, while the database integration test verifies that repeated processing of the same bar does not create additional fills.

This is **not** an exchange matching engine. The queue and rejection outcomes are reproducible approximations, not fitted venue probabilities. Bar-based fills are priced using the completed bar's close plus an adverse slippage estimate; they do not reconstruct order-book queue priority, intrabar path, or exact execution time.

## Cost calculation

When context is available, modeled adverse slippage is:

`base profile slippage + half the supplied spread + estimated market impact`

Market impact is a square-root participation heuristic:

`coefficient × per-bar return volatility × sqrt(order quantity / observed bar volume) × 10,000`

Impact is capped by `PAPER_MAX_MARKET_IMPACT_BPS`. Defaults are recorded in `.env.example`; they are simulation assumptions, **not venue-calibrated facts**. Every cost model result retains `calibration_status=UNVALIDATED_HEURISTIC`. Missing, malformed, stale or unverified liquidity must not be represented as verified execution liquidity.

## Configuration controls

- `PAPER_FILL_LATENCY_BARS`: number of new completed bars before fill attempts begin (default 1).
- `PAPER_ORDER_TTL_BARS`: maximum number of fresh bars before remaining quantity expires (default 24).
- `PAPER_MAX_BAR_VOLUME_PARTICIPATION`: maximum portion of verified bar volume that all matching orders can consume together (default 0.10).
- `PAPER_QUEUE_FILL_PROBABILITY`: deterministic queue-pass threshold (default 0.80).
- `PAPER_REJECTION_PROBABILITY`: deterministic rejection threshold (default 0.01).
- `PAPER_MARKET_IMPACT_COEFFICIENT` and `PAPER_MAX_MARKET_IMPACT_BPS`: uncalibrated impact heuristic controls.

Because outcomes derive from order/bar keys, retrying the same order on the same bar returns no new fill. Changing a probability or using a synthetic context for tests does not calibrate the model.

## Known limitations and release gates

- Venue-cost calibration still requires held-out observed fills, spreads, order sizes and reliable liquidity/volume units by instrument.
- The bar-level fill model is only an approximation; exact queue position, cancellations and intrabar sequencing remain outside its proven scope.
- Crypto research still lacks an authoritative point-in-time universe with delisted assets.
- Promotion-grade Deflated Sharpe Ratio still requires a complete, comparable historical trial-performance archive.
- FX/commodity fills do not use OANDA candle counts as liquidity and therefore remain non-fillable by this crypto-volume model; OANDA is practice/demo-only.
- Customer paper execution remains blocked until a separate transactional simulation ledger passes account-isolation, idempotency, recovery and concurrency tests.
- No simulator test, synthetic market data or green CI job authorizes live trading, deposits, withdrawals or customer-money movement.

## Verification

The simulation suite tests cost-model monotonicity, single-order low-liquidity partial fills, aggregate liquidity caps under 500 synthetic orders, new-bar idempotency, latency, queue misses, rejection, TTL, unverified FX/OTC volume, and end-to-end database order-state updates. The complete mandatory CI and PostgreSQL concurrency suite must pass on the exact final candidate commit before merge.
