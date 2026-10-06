# Live trading evidence record

This document is an evidence template, not a performance claim. Do not mark a gate PASS without attaching the underlying logs, transaction/order IDs, timestamps, configuration SHA, and test environment.

## A. Exact configuration
- Git commit:
- Model artifact/version:
- Customer live exchange: Binance
- Customer live market type: Spot
- Customer live trading enabled:
- Paper trading:
- Broker sandbox:
- Pilot stage:
- Pilot risk caps:
- Test start/end UTC:

## B. Out-of-sample paper evidence
Required before customer-money activation.
- Dataset source:
- Symbol(s):
- Timeframe:
- OOS period:
- Number of OOS trades:
- Net return after fees/slippage:
- Maximum drawdown:
- Sharpe:
- Win rate:
- Turnover:
- 2x cost-stress result:
- DSR / confidence statistic:
- Exact research/backtest command or API request:
- Output artifact:
- Reviewer:

Do not enter estimated or fabricated values. If not run, record NOT RUN.

## C. Sandbox/testnet evidence
- Venue:
- Sandbox/testnet account:
- Connectivity:
- Market-data freshness:
- Open order:
- Fill:
- Duplicate clientOrderId:
- Cancel:
- Double-cancel:
- Fee extraction:
- Reduce-only close (if derivatives drill is applicable):
- Worker crash/restart during fill:
- Post-restart reconciliation:
- Ledger invariant after drill:
- Evidence links / order IDs:
- Reviewer:

A skipped drill is not a pass. If not run, record NOT RUN.

## D. Custody evidence
- TRON test transaction ID:
- Deposit address:
- Confirmation height:
- Customer ledger credit ID:
- Sweep intent ID:
- Custody signer transaction ID:
- Destination custody/account:
- Binance customer sub-account identifier:
- Binance balance observation:
- Reconciliation result:
- Evidence reviewer:

Never record private keys, seed phrases, HMAC secrets, or exchange API secrets here.

## E. Release/deployment evidence
- CI run IDs:
- Image digest:
- Cloud Run revision:
- healthz result:
- readyz result:
- Immutable release SHA:
- Canary result:
- Rollback result:
- Backup restore drill:
- Alert/incident drill:

## Gate decision
- [ ] CI and mandatory tests pass with zero unexpected skips
- [ ] Sandbox/testnet drill passed
- [ ] Worker crash/reconciliation drill passed
- [ ] Minimum three months exact-configuration OOS paper evidence passed
- [ ] 2x cost stress passed
- [ ] Custody-to-Binance staging drill passed
- [ ] Customer isolation/auth/MFA acceptance passed
- [ ] Pilot limits verified
- [ ] Deployment/canary/rollback evidence passed
- [ ] Legal/regulatory review completed

**Final decision:** NO-GO until every applicable item has evidence.