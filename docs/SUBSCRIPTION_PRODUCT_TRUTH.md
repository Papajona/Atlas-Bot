# Atlas Subscription Product Truth

Status: Release reference document.
Source of truth: app/main.py, app/db.py, billing migrations, customer UI, and repository tests.

A benefit is advertisable only when plan definition, customer description, server-side entitlement/limit enforcement, implementation, regression coverage, and required staging E2E evidence exist.

## Current plan configuration

| Plan | Monthly | Annual | AI credits | Exchanges | Strategies | Live flag | Paper |
|---|---:|---:|---:|---:|---:|---|---|
| Free | $0 | $0 | 100 | 0 | 0 | No | Yes |
| Starter | $7.99 | $79.90 | 1,000 | 1 | 1 | Yes* | Yes |
| Pro | $17.99 | $179.90 | 5,000 | 3 | 5 | Yes* | Yes |
| Elite | $39.99 | $399.90 | 20,000 | 10 | 20 | Yes* | Yes |

*The live flag is only one entitlement. It does not bypass deployment, risk, security, broker, kill-switch, account, or live-trading gates. The current paper/sandbox deployment must not be marketed as live-money availability.

## Implemented and server-gated

- market scanner
- Smart Trade paper planning
- DCA bot
- Grid bot paper path
- alerts configuration
- arbitrage paper/research path
- Strategy Builder / Strategy Lab
- customer webhooks

## Existing but not proven as a complete paid entitlement

- daily research
- advanced AI
- risk engine
- portfolio analytics
- multi-timeframe research
- walk-forward research
- portfolio risk
- trading journal
- API access
- priority support
- AI credit allowance enforcement
- exchange-count enforcement
- active-strategy-count enforcement

These must not be represented as hard plan limits or exclusive paid benefits until server-side enforcement and regression tests exist.

## Operational truth

The repository documents that alerts are saved as CONFIGURED when no evaluator/notification worker is available. Smart Trade and Grid creation are paper-mode operations. Binance triangular-arbitrage live execution is disabled until its multi-leg lifecycle is integrated into the unified execution/ledger/recovery path.

Use precise wording such as paper, research, configured, or subject to approval.

## Billing infrastructure

The repository contains subscription persistence, Stripe checkout/webhook handling, referral attribution/commission records, revenue/cost records, and customer/admin billing routes. Production billing still requires real Stripe secrets, price IDs, webhook delivery, and invoice reconciliation in staging.

## Release policy

Do not add a pricing/marketing benefit unless the entitlement matrix is updated and its negative test proves a lower-tier customer is denied the protected action.
