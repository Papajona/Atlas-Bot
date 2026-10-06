# Four-Week Forward Paper/Shadow Validation Gate

## Purpose

Atlas uses a four-week forward paper/shadow period as an **interim production-readiness gate**. This does not replace the stronger 90-day evidence requirement; it provides a shorter, explicitly labelled evidence checkpoint.

## Rules

The 28-day period must use the exact release/configuration intended for the next promotion decision.

- Minimum forward duration: **28 consecutive calendar days**.
- Execution mode: **PAPER/SHADOW only**. No customer live orders.
- Market data: real forward market data from the configured research/execution venue.
- Research and release identity: record the release commit SHA and configuration identity.
- Costs: use verified fee evidence and the configured slippage model.
- Promotion stress: the existing 2x cost-stress gate remains mandatory.
- Deflated Sharpe Ratio: production floor remains **>= 0.95**.
- Risk controls: drawdown, daily-loss, leverage, position, stale-data and edge-decay controls must remain active.
- Incidents: API outages, reconnects, missing data, reconciliation failures and kill-switch events must be recorded and resolved.
- Evidence: paper/shadow decisions, fills, costs, P&L and gate decisions must be retained and reproducible.

## Status interpretation

| Evidence | Status |
|---|---|
| <28 days | NOT READY — insufficient interim forward evidence |
| >=28 days and all interim gates pass | CONDITIONAL — four-week evidence gate passed |
| >=90 days and all final gates pass | FINAL — full forward-evidence requirement satisfied |

A 28-day pass must **not** be represented as a 90-day pass.

## Required evidence record

At minimum retain:

- start/end timestamps;
- release commit SHA;
- configuration hash;
- data/source identity and dataset hash;
- strategy/model identity;
- paper/shadow trades and realized outcomes;
- actual/verified fees and slippage assumptions;
- 2x cost-stress result;
- DSR and trial-count evidence;
- drawdown and daily-loss observations;
- risk/kill-switch events;
- reconciliation status;
- unresolved incident count.

## Production boundary

The four-week period is a forward validation exercise, not authorization for live trading. Live trading remains fail-closed until the applicable production controls and external evidence are independently verified.
