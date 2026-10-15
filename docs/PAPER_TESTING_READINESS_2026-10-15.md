# Atlas-Bot Paper-Testing Readiness — 15 October 2026

## Disposition

**NOT READY for customer paper testing or validation-grade paper performance claims.** This patch improves the platform-level paper simulator and closes unsafe research-promotion paths. It does not enable customer paper execution, live orders, funding, payouts, or model promotion.

## Repairs in this change

- Platform paper fills can use a volume-participation / recent-volatility slippage estimate when the server passes normalized market context and traceable calibration identifiers.
- The paper model can reproduce order rejection and latency outcomes from configured, traceable calibration inputs. Rejection and latency draws are deterministic per order key so retries are reproducible; latency-dependent adverse cost uses a configured coefficient and an explicit square-root-time approximation.
- Fill quantity is capped by the configured share of observed tradable volume. Unfilled residual quantity is recorded as cancelled, not falsely reported as filled.
- Staging/production paper orders are rejected if required market context or calibration evidence is missing. Local development retains an explicitly labelled static-profile fallback for tests and development only.
- Paper position updates apply the new fill before advancing the cumulative filled quantity; positions are searched by the trade execution mode instead of being hard-coded to LIVE. Ambiguous duplicate legacy positions fail closed and are not automatically merged.
- Research OOS results expose the unresolved current-survivor universe and incomplete static-bps slippage model. The promotion gate refuses to promote a result explicitly tagged with unresolved crypto survivorship bias or an incomplete slippage model.
- Meta-label OOS diagnostics and source-bar purge/embargo were merged separately in PR #126 after exact-head CI passed.

## Required configuration and market context

The following paper-impact configuration is intentionally unset by default:

- `PAPER_IMPACT_MAX_PARTICIPATION_RATE`
- `PAPER_IMPACT_COEFFICIENT`
- `PAPER_EXECUTION_REJECTION_PROBABILITY`
- `PAPER_EXECUTION_LATENCY_MIN_MS`
- `PAPER_EXECUTION_LATENCY_MEDIAN_MS`
- `PAPER_EXECUTION_LATENCY_P95_MS`
- `PAPER_EXECUTION_LATENCY_MAX_MS`
- `PAPER_LATENCY_IMPACT_COEFFICIENT`
- `PAPER_EXECUTION_EVIDENCE_SOURCE`
- `PAPER_EXECUTION_EVIDENCE_PATH`
- `PAPER_EXECUTION_EVIDENCE_ID`

The internal `paper_market_context` must include:

- `tradable_volume_units`: server-observed volume normalized to the instrument's order quantity units;
- `spread_bps`: observed bid/ask spread in basis points;
- `recent_volatility_bps`: recent realized volatility in basis points;
- `bar_duration_ms`: the duration of the candle used to estimate the recent volatility.

These values must come from a server-validated market-data path. They must not be trusted if supplied unchecked by a client. The model coefficients, participation ceiling, rejection probability, latency quantiles and evidence identifiers must be calibrated and reviewed before staging use. The evidence path must point to a readable calibration artifact; the evidence ID must be its actual SHA-256 digest (with an optional `sha256:` prefix). Placeholder values or a CI fixture do not establish real venue calibration.

## Limitations that remain open

1. **Paper market-data plumbing:** the platform `/api/execute` and `/api/paper/step` paths now attempt to build `paper_market_context` from server-fetched completed CCXT spot candles, a currently timestamped bid/ask ticker, and a listed active spot market. Non-spot/derivative, FX, and commodity contexts intentionally fail closed until instrument volume units are independently normalized. Customer paper execution remains blocked separately by the isolated-ledger guard.
2. **Execution realism:** latency and venue rejection can now be simulated only if measured quantiles, rejection probability, impact coefficient and traceable evidence are configured. These fields are unset by default, so staging/production rejects paper orders until calibration is supplied. Order-book queue position and depth, retry/requote behaviour, and intrabar market movement during simulated latency remain unmodelled. The result stays tagged `INCOMPLETE_EXECUTION_MODEL` and is not validation-grade.
3. **Research model parity:** research backtests still use fixed slippage bps. They explicitly report `slippage_model_complete=false`, and the promotion gate blocks them until a volume/volatility-aware cost model is used in research too.
4. **Customer paper-account isolation:** customer paper orders remain blocked until there is a segregated simulation ledger and per-customer/session positions, trades, and equity. Do not remove `customer_paper_execution_blocked` to bypass this gate.
5. **Survivorship bias:** no verified point-in-time historical crypto universe or delisted-asset history is supplied by this code change. Research reports `CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY`, and promotion is blocked on that explicit status.
6. **External evidence:** financing/carry schedules, original dataset provenance, comparable trial-performance archives/DSR, real staging database identity, provider reconciliation, and a real restore drill remain separate evidence gates.

## Safety

All changes are paper/research controls only. `PAPER_TRADING` remains the default, live trading stays disabled, customer-live execution remains fail-closed, and no mainnet funding, withdrawals, or payouts are enabled.
