# Atlas-Bot Paper-Testing Readiness — 15 October 2026

## Disposition

**NOT READY for customer paper testing or validation-grade paper performance claims.** This patch improves the platform-level paper simulator and closes unsafe research-promotion paths. It does not enable customer paper execution, live orders, funding, payouts, or model promotion.

## Repairs in this change

- **Calibrated fill-model integration is now implemented in the active platform and autonomous customer-paper paths.** `app/paper_execution_model.py` is called by `advance_platform_paper_orders` and the autonomous customer-paper `_fill_open_order` path when the complete SHA-pinned runtime calibration configuration is present. The platform path obtains the spread/volatility/quote context from `_server_paper_market_context`; the customer path uses the validated server-fetched closed spot candles and fresh ticker. Both pass the persisted shared-bar capacity into the estimator.
- Runtime calibration configuration is all-or-nothing. If some but not all settings are configured, the request fails closed. Runtime calls prohibit test-only fixture evidence. A configured model must verify the calibration artifact SHA-256, dataset SHA-256, venue/symbol coverage, observation counts and exact configured parameter values before estimating a fill.
- Model evidence (calibration digest, deterministic latency/rejection draws, model version/status, modeled costs, fill amount and cancelled remainder) is persisted with platform fill audit events and with customer-paper fill, journal and order-event metadata. Customer same-bar retry reads the immutable evidence and replays the prior decision instead of recalculating it.
- The platform and autonomous customer paths retain existing kill-switch, verified spot-volume, shared capacity, cash/risk budget, append-only ledger and idempotency controls. Calibrated residual quantity is explicitly canceled; it is never represented as filled.
- **Manual customer API advancement remains a separate, explicitly heuristic OHLCV-replay path.** Historical candles do not contain a timestamped bid/ask spread, so this path cannot claim to use the calibrated spread/latency model. Its output must remain diagnostic and non-validation-grade until it receives verifiable quote evidence or is routed through the calibrated autonomous path.
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

1. **Market-data coverage:** the platform's calibrated path requires fresh server-observed CCXT spot quotes and completed candles. The autonomous customer-paper path requires the same supported spot venue/symbol context. FX, commodities, derivatives, unsupported CCXT markets, stale quotes, incomplete bars, and mismatched provenance fail closed when calibrated execution is configured.
2. **Execution realism:** the calibrated estimator is wired into the active platform and autonomous customer-paper fills, but queue position/depth, order-book replenishment, retry/requote behaviour, and intrabar movement during simulated latency remain unmodelled. The estimator intentionally continues to label the result `INCOMPLETE_EXECUTION_MODEL`; integration does not convert modeled costs into proof of venue-realistic fills or validation-grade performance. The manual customer API OHLCV-replay path remains heuristic because it does not have historical bid/ask quotes.
3. **Research model parity (conditional implementation):** research and strategy-router returns now accept the same shared spread/impact/latency cost kernel used by the calibrated paper estimator. The research path requires a signed per-bar spread series bound to the exact dataset and UTC bar index, verified CCXT spot base-volume units, the SHA-pinned paper calibration artifact, reviewed spread/slippage error bounds, and an explicit `RESEARCH_ORDER_NOTIONAL_USD`. It remains diagnostic-only whenever any input is missing or a modeled event exceeds the participation ceiling; non-zero rejection probability remains blocked until rejected-order state is modelled. This code path does not supply venue evidence or enable promotion by itself. OHLC-only stop/target ordering remains an independent incomplete-execution gate.
4. **Customer paper-account isolation:** the controller integration now routes customer paper crypto spot orders only through the isolated append-only paper ledger with per-customer virtual balances, positions, idempotency, restart recovery and protective exits. This is available only after the opt-in configuration is set and the database/role/RLS preflight passes; all flags remain disabled by default. A green CI run is not an operational staging drill, and the simulated cost assumptions are not venue-calibrated evidence. Do not enable the feature until reviewed staging isolation and zero-change-to-real-ledger checks pass.
5. **Survivorship bias:** no verified point-in-time historical crypto universe or delisted-asset history is supplied by this code change. Research reports `CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY`, and promotion is blocked on that explicit status.
6. **External evidence:** financing/carry schedules, original dataset provenance, comparable trial-performance archives/DSR, real staging database identity, provider reconciliation, and a real restore drill remain separate evidence gates.

## Safety

All changes are paper/research controls only. `PAPER_TRADING` remains the default, live trading stays disabled, customer-live execution remains fail-closed, and no mainnet funding, withdrawals, or payouts are enabled.
