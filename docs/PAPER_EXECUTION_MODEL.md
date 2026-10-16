# Platform paper-execution model — implementation and evidence limits

## Active path

The operator route `POST /api/paper/step` submits platform-only paper orders and advances existing orders against market bars. A newly submitted order is persisted as `OPEN` with zero filled quantity. It cannot fill on its submission bar. A later, new completed bar can produce a fill attempt. `POST /api/execute` cannot bypass this lifecycle: paper execution without server-bound completed-bar and data-provenance context is rejected. Customer paper execution remains blocked until a customer-isolated transactional simulation ledger is implemented and tested.

The active lifecycle is persisted in `paper_order_states`, `paper_bar_states`, and `paper_fill_events`. Database uniqueness constraints prevent duplicate order/bar attempts and duplicate exchange/symbol/timeframe/bar budgets. The executor locks pending orders and the shared bar budget in one transaction. Replaying the same bar does not create another fill or consume its volume twice. A repeated bar with conflicting provenance/price/volume is rejected rather than mixing capacity from one payload with prices from another. To avoid counting overlapping bar volumes more than once, only one active paper timeframe per instrument is allowed until a cross-timeframe liquidity allocator is implemented.

## Fill lifecycle and costs

- The simulator waits for the configured number of new completed bars, then evaluates each still-open order once on that bar.
- A verified bar has one aggregate participation budget shared by all matching orders. A fill is capped by remaining order quantity and remaining bar capacity, so orders can partially fill across bars.
- Deterministic SHA-256 outcomes model queue misses and rejections. Orders that cannot fill expire at the configured completed-bar TTL; any remaining quantity is canceled, while previously filled quantity and position remain recorded.
- Fill prices use the completed bar close adjusted adversely by the asset profile's baseline slippage plus a bounded participation/volatility impact heuristic. Taker fees are charged per partial fill. Order average fill price is quantity weighted.
- Queue/rejection probabilities and impact coefficients are **simulation assumptions, not venue-calibrated facts**. No order-book spread is currently modeled. This is not an exchange matching engine and does not reconstruct intrabar price paths or queue position.

Research reports must keep `execution_cost_model_complete=false` and report `execution_cost_calibration_status=MISSING_VENUE_CALIBRATION_EVIDENCE` until a trusted, venue-specific calibration archive is implemented and independently verified. Configured fees, synthetic stress runs, carry-rate evidence, and deterministic impact coefficients do not satisfy this requirement. Promotion also requires a non-empty traceable `execution_cost_calibration_evidence_id`; the gate rejects a bare boolean claim.
- Stop-loss/take-profit triggers are evaluated from completed-bar OHLC. If both levels are touched in one bar, stop-loss takes precedence. A trigger creates a persisted exit order that cannot fill until a later completed bar; it must still pass verified spot-volume and capacity checks. Stop-loss exits do not expire on TTL, and stop fills account for adverse opening gaps. This is a conservative bar-based heuristic, not tick-level stop execution.

## Liquidity gate

A new platform paper order is accepted only when the requested asset is crypto and the server-bound market data comes from `ccxt_ohlcv`, explicitly identifies a non-contract spot market, marks volume as verified `base_asset`, and matches the exact symbol/exchange/timeframe submitted. Each bar must also have valid OHLCV and stable provenance. Capacity is `bar_volume × PAPER_MAX_BAR_VOLUME_PARTICIPATION`.

Swap/futures/contract markets, unknown exchange metadata, Yahoo Finance volumes, and OANDA/OTC candles are **not** treated as executable base-asset liquidity. New paper orders for FX, commodities, derivatives, or unknown markets are rejected before a Trade row is created. Any legacy or previously persisted order that lacks verified liquidity remains unfilled; ordinary entry/target orders expire under TTL, while a triggered stop-loss exit remains pending rather than pretending it was executed. FX/commodity data can still support research/practice paths, but this simulator does not claim their fills are realistic.

## Configuration controls

- `PAPER_FILL_LATENCY_BARS` (default 1): minimum number of new completed bars before a fill attempt.
- `PAPER_ORDER_TTL_BARS` (default 24): maximum number of new completed bars before any remainder is canceled.
- `PAPER_MAX_BAR_VOLUME_PARTICIPATION` (default 0.10): aggregate capacity as a fraction of verified bar volume.
- `PAPER_QUEUE_FILL_PROBABILITY` (default 0.80): deterministic queue-pass heuristic.
- `PAPER_REJECTION_PROBABILITY` (default 0.01): deterministic rejection heuristic.
- `PAPER_MARKET_IMPACT_COEFFICIENT` (default 0.10) and `PAPER_MAX_MARKET_IMPACT_BPS` (default 250): bounded, uncalibrated impact estimate.

## Still-open evidence gates

- Crypto research candidates remain blocked. No trusted point-in-time asset-universe loader and manifest validator currently proves that delisted/failed assets are represented. A caller-supplied status string is not accepted as proof.
- Promotion-grade Deflated Sharpe Ratio remains blocked without a complete comparable historical trial-performance archive evaluated on the same held-out samples.
- Missing, malformed or unverified financing/carry evidence continues to block promotion.
- FX/commodities still lack verified venue/product-specific fill liquidity, complete market-calendar/rollover treatment and end-to-end instrument-specific financing/margin evidence.
- The simulator cost model is behaviorally testable but not calibrated against held-out observed venue fills, spread, order sizes, and verified liquidity for each instrument. The machine-readable promotion state remains `MISSING_VENUE_CALIBRATION_EVIDENCE`.

Synthetic tests and green CI verify software behavior only. They do not authorize live trading, deposits, withdrawals, custody of customer funds, or production launch. Live trading, payouts, and customer-money movement must remain disabled until their separate release evidence gates pass.
