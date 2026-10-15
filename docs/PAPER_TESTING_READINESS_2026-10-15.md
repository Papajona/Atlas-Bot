# Atlas-Bot Paper-Testing Readiness — 15 October 2026

## Disposition

**NOT READY for customer paper testing or validation-grade paper performance claims.** This patch improves the platform-level paper simulator and closes unsafe research-promotion paths. It does not enable customer paper execution, live orders, funding, payouts, or model promotion.

## Repairs in this change

- Platform paper fills can use a volume-participation / recent-volatility slippage estimate when the server passes normalized market context and traceable calibration identifiers.
- Fill quantity is capped by the configured share of observed tradable volume. Unfilled residual quantity is recorded as cancelled, not falsely reported as filled.
- Staging/production paper orders are rejected if required market context or calibration evidence is missing. Local development retains an explicitly labelled static-profile fallback for tests and development only.
- Paper position updates apply the new fill before advancing the cumulative filled quantity; positions are searched by the trade execution mode instead of being hard-coded to LIVE. Ambiguous duplicate legacy positions fail closed and are not automatically merged.
- Research OOS results expose the unresolved current-survivor universe and incomplete static-bps slippage model. The promotion gate refuses to promote a result explicitly tagged with unresolved crypto survivorship bias or an incomplete slippage model.
- Meta-label OOS diagnostics and source-bar purge/embargo were merged separately in PR #126 after exact-head CI passed.

## Required configuration and market context

The following paper-impact configuration is intentionally unset by default:

- `PAPER_IMPACT_MAX_PARTICIPATION_RATE`
- `PAPER_IMPACT_COEFFICIENT`
- `PAPER_EXECUTION_EVIDENCE_SOURCE`
- `PAPER_EXECUTION_EVIDENCE_ID`

The internal `paper_market_context` must include:

- `tradable_volume_units`: server-observed volume normalized to the instrument's order quantity units;
- `spread_bps`: observed bid/ask spread in basis points;
- `recent_volatility_bps`: recent realized volatility in basis points.

These values must come from a server-validated market-data path. They must not be trusted if supplied unchecked by a client. The model coefficient, participation ceiling, and evidence identifier must be calibrated and reviewed before staging use. Placeholder values or a CI fixture do not establish a real venue calibration.

## Limitations that remain open

1. **Paper market-data plumbing:** all staging/production paper call sites do not yet attach server-validated `paper_market_context`. Until each required path provides it, those orders fail closed rather than falling back to static slippage.
2. **Execution realism:** latency, order-book queue position, venue rejection probability, and retry/requote behaviour are not yet modeled. The new fill result is deliberately tagged `INCOMPLETE_EXECUTION_MODEL` and is not validation-grade.
3. **Research model parity:** research backtests still use fixed slippage bps. They explicitly report `slippage_model_complete=false`, and the promotion gate blocks them until a volume/volatility-aware cost model is used in research too.
4. **Customer paper-account isolation:** customer paper orders remain blocked until there is a segregated simulation ledger and per-customer/session positions, trades, and equity. Do not remove `customer_paper_execution_blocked` to bypass this gate.
5. **Survivorship bias:** no verified point-in-time historical crypto universe or delisted-asset history is supplied by this code change. Research reports `CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY`, and promotion is blocked on that explicit status.
6. **External evidence:** financing/carry schedules, original dataset provenance, comparable trial-performance archives/DSR, real staging database identity, provider reconciliation, and a real restore drill remain separate evidence gates.

## Safety

All changes are paper/research controls only. `PAPER_TRADING` remains the default, live trading stays disabled, customer-live execution remains fail-closed, and no mainnet funding, withdrawals, or payouts are enabled.
