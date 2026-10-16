# Platform paper-execution model — current verified status

## Current status

**Bar-driven paper execution is not yet implemented in the active execution path.** The previous documentation described delayed fills, partial fills, aggregate volume caps, queue outcomes, TTL and market-impact controls; inspection of the active `app/execution.py::execute_signal` path found none of those controls wired into order processing.

The old branch immediately filled the requested quantity at a fixed profile slippage rate and marked it `SIMULATED`. That path has now been changed to fail closed with a `RiskBlocked` error before creating a trade or changing positions/equity. This is intentional: an unimplemented simulator must not produce misleading paper-forward results.

Customer paper execution remains separately blocked until a customer-isolated transactional simulation ledger is implemented and tested. Live trading, customer funds, withdrawals and mainnet custody remain outside the scope of any paper-execution change.

## Required simulator before re-enabling platform paper execution

A proper implementation must accept a new order as unfilled and only attempt fills on a later, unique, completed market bar. It must persist the bar/order idempotency key and lifecycle state; account for configured latency; allow partial fills; enforce aggregate participation limits only when source provenance and volume units are verified; apply deterministic, bounded queue/rejection and TTL handling; and never reuse the same bar to create additional fills.

Adverse cost estimates may combine profile slippage, half-spread and a bounded participation/volatility impact estimate. Until calibrated against held-out venue fills and instrument-specific liquidity units, all such values must be labelled unvalidated heuristics—not measured execution costs. OTC/OANDA candle counts and Yahoo volume must not be treated as executable crypto base-asset liquidity.

## Research and market-data gates

- Crypto strategy candidates are blocked unless the research record carries explicit `VERIFIED_POINT_IN_TIME_UNIVERSE` evidence. A current established-coin allowlist is not a historical universe and omits delisted/failed assets.
- Promotion-grade Deflated Sharpe Ratio still requires a complete, comparable historical trial-performance archive measured on the same held-out samples.
- Missing, malformed or unverified carry/financing evidence must continue to block promotion.
- FX and commodity instruments require distinct venue/product-specific execution, calendar, precision, liquidity and financing evidence; crypto volume assumptions are not transferable.
- Synthetic tests and green CI jobs verify software behavior only. They do not calibrate fill assumptions or authorize live trading, deposits, withdrawals or customer-money movement.

## Verification contract

Before the paper path is re-enabled, the active path—not just a disconnected model helper—must be covered by tests for partial fills, low and missing liquidity, aggregate caps across concurrent orders, bar/order idempotency, latency, queue misses, rejections, cancellation/TTL, restart recovery, and database concurrency. The complete mandatory CI and PostgreSQL concurrency suite must pass on the exact candidate commit. Customer paper must remain blocked until account isolation and transactional ledger tests also pass.
