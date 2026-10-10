# Forex Broker Integration — OANDA v20

## Purpose and supported environment

Atlas uses OANDA v20 for Forex/commodity research, backtesting, and explicitly enabled practice/demo execution. The adapter is practice-only: `OandaConfig(practice=False)` is rejected, and the adapter's API base URL is fixed to `https://api-fxpractice.oanda.com`. The production host `https://api-fxtrade.oanda.com` is documented here for distinction only; Atlas must not submit orders to it.

## Official API environments

- Practice: `https://api-fxpractice.oanda.com`
- Production: `https://api-fxtrade.oanda.com` — **not supported by the Atlas adapter**

All Atlas execution validation must use an OANDA practice account and practice credentials.

## Credentials

Create a v20-enabled OANDA practice account and API token through OANDA's account management process. Keep the token server-side in the approved secret store; never commit it or place it in Android/browser code.

## Supported application flow

1. Fetch account state.
2. Fetch current bid/ask and verify quote freshness.
3. Fetch completed candles.
4. Generate the research/demo signal.
5. Apply portfolio and risk gates.
6. Normalize Forex units and validate instrument limits.
7. Submit a practice market order with a deterministic client ID.
8. Attach stop-loss/take-profit on fill when configured.
9. Record OANDA transaction IDs.
10. Reconcile order and account/position state.

## Failure semantics

A transport-layer failure during submission is `UNKNOWN`, not `FAILED`: OANDA may have accepted the request before the connection failed. Do not automatically retry the order. Reconcile using the deterministic client order ID and OANDA transaction/order evidence first.

## Live-money gate

**OANDA live Forex/commodity execution is disabled and unsupported in Atlas.** The current adapter permanently rejects `practice=False`; changing an environment flag is not a supported route to live OANDA. Do not describe OANDA live execution as available or promote it after practice testing. Any future live-broker integration would require a separately reviewed adapter, explicit risk/ledger integration, and its own approval.

An actual practice-account order acceptance/fill/reconciliation drill remains separate operational evidence; unit tests or a successful build do not prove that drill occurred.

## Practice/demo hardening

- `/api/forex/demo/preflight` verifies practice account access, instrument tradeability, executable bid/ask, and quote freshness.
- `/api/forex/demo/price` is a read-only practice quote endpoint.
- Demo order quantity is capped by `OANDA_DEMO_MAX_UNITS` (default 100,000).
- Demo quote freshness is capped by `OANDA_DEMO_MAX_QUOTE_AGE_SECONDS` (default 3 seconds).
- The demo path remains practice-only and never selects the production OANDA host.
- OANDA personal tokens remain server-side; do not place them in Android/browser code.
