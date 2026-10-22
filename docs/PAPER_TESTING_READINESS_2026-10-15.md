# Atlas-Bot Paper-Testing Readiness — 15 October 2026

## Disposition

**NOT READY for customer paper testing or validation-grade paper performance claims.** This patch improves the platform-level paper simulator and closes unsafe research-promotion paths. It does not enable customer paper execution, live orders, funding, payouts, or model promotion.

## Repairs in this change

- **Calibrated fill-model integration remains incomplete.** `app/paper_execution_model.py` contains the standalone volume/volatility-aware estimator, traceable calibration checks, and deterministic rejection/latency draws, but no active runtime fill path imports or calls that model. Its `PAPER_IMPACT_*`, `PAPER_EXECUTION_*`, and `PAPER_LATENCY_*` inputs do not affect fills today.
- Customer-paper controller/submission paths call `_server_paper_market_context` to validate server-fetched completed crypto spot candles and a fresh quote. That helper validates market context; it does **not** call the calibrated fill model.
- The platform `/api/paper/step` route advances orders through `advance_platform_paper_orders` in `app/execution.py`, which uses a separate bar-driven simulator with heuristic queue, rejection, latency, participation, and impact settings. Fill quantity is capped by verified bar-volume capacity; unfilled residual quantity is cancelled rather than reported as filled.
- Staging/production do not currently reject platform paper orders because calibrated-model evidence is absent: that model is not wired into the active platform fill path. The active simulator enforces its own completed-bar and data-provenance gates. Customer-paper execution separately requires its isolated-ledger/configuration preflight and verified market context; that is not evidence of calibrated execution costs.
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

These values must come from a server-validated market-data path. They must not be trusted if supplied unchecked by a client. The model coefficients, participation ceiling, rejection probability, latency quantiles and evidence identifiers must be calibrated and reviewed before staging use. The evidence path must point to a readable calibration artifact; the evidence ID must be its actual SHA-256 digest (with an optional `sha256:` prefix). The artifact must declare the required schema, provenance source, source-data digest, positive observation counts, and exact covered venue/symbol. Its calibrated parameter object must match the configured base slippage, participation, impact, rejection and latency values to a strict numeric tolerance. The configured evidence source must match the artifact's source identifier. Test-only fixtures are accepted only by an explicit development-only override, never by staging/production. Placeholder values or a CI fixture do not establish real venue calibration.

## Limitations that remain open

1. **Paper market-data plumbing:** customer-paper controller/submission paths call `_server_paper_market_context` to validate server-fetched completed CCXT crypto spot candles and a fresh quote, and fail closed when that supported context is unavailable. Platform `/api/paper/step` does not call that helper; it calls `advance_platform_paper_orders`, which independently checks bar completeness, provenance, and verified liquidity before filling. Calling the context helper does not connect the calibrated fill model.
2. **Execution realism:** the standalone calibrated latency/rejection/impact model is not wired into the active platform simulator or customer-paper fill-cost path. Its optional calibration settings therefore do not change runtime fills. The active platform simulator still uses separate heuristic settings; queue position/depth, retry/requote behaviour, and intrabar movement during simulated latency remain unmodelled. Results stay tagged `INCOMPLETE_EXECUTION_MODEL` and are not validation-grade.
3. **Research model parity:** research backtests still use fixed slippage bps. They explicitly report `slippage_model_complete=false`, and the promotion gate blocks them until a volume/volatility-aware cost model is used in research too.
4. **Customer paper-account isolation:** the controller integration now routes customer paper crypto spot orders only through the isolated append-only paper ledger with per-customer virtual balances, positions, idempotency, restart recovery and protective exits. This is available only after the opt-in configuration is set and the database/role/RLS preflight passes; all flags remain disabled by default. A green CI run is not an operational staging drill, and the simulated cost assumptions are not venue-calibrated evidence. Do not enable the feature until reviewed staging isolation and zero-change-to-real-ledger checks pass.
5. **Survivorship bias:** no verified point-in-time historical crypto universe or delisted-asset history is supplied by this code change. Research reports `CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY`, and promotion is blocked on that explicit status.
6. **External evidence:** financing/carry schedules, original dataset provenance, comparable trial-performance archives/DSR, real staging database identity, provider reconciliation, and a real restore drill remain separate evidence gates.

## Safety

All changes are paper/research controls only. `PAPER_TRADING` remains the default, live trading stays disabled, customer-live execution remains fail-closed, and no mainnet funding, withdrawals, or payouts are enabled.
