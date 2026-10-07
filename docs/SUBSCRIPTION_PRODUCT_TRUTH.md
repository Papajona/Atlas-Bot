# Atlas Subscription Product Truth

Status: current-main release reference after PR #60.

A benefit is advertisable only when the plan definition, customer description, server-side enforcement, implementation, regression coverage, and required staging E2E evidence agree.

## Current plan configuration

| Plan | Monthly | Annual | AI credits | Exchanges | Strategies | Live flag | Paper |
|---|---:|---:|---:|---:|---:|---|---|
| Free | $0 | $0 | 100 | 0 | 0 | No | Yes |
| Starter | $7.99 | $79.90 | 1,000 | 1 | 1 | Yes* | Yes |
| Pro | $17.99 | $179.90 | 5,000 | 3 | 5 | Yes* | Yes |
| Elite | $39.99 | $399.90 | 20,000 | 10 | 20 | Yes* | Yes |

*The live flag is an entitlement field only. It does not bypass global live controls, broker restrictions, risk gates, kill switch, account mapping, deployment state, or operational approval.

## Verified server enforcement on current main

- Exchange connection count: enforced for new OANDA and Deriv customer connections and for new Binance customer provisioning.
- Active strategy/executor count: enforced when creating a new adaptive customer bot or executor.
- AI allowance: atomically metered for Strategy Builder generation after successful AI generation.
- Portfolio analytics: server-gated by portfolio_analytics.
- Portfolio risk analytics: server-gated by portfolio_risk.
- Customer entitlement/usage endpoint: available at /api/customer/subscription/entitlements.

These are code/test verified. They are not a substitute for staging E2E evidence.

## Not yet proven as complete paid entitlements

The plan catalog still contains capabilities whose complete customer-facing/service lifecycle is not established by the repository alone:

- daily research
- advanced AI as a distinct paid entitlement
- multi-timeframe research
- walk-forward research
- trading journal
- API access
- priority support

Do not market these as exclusive, fully delivered paid benefits until the entitlement, implementation, regression, and operational evidence are complete.

## Product safety

Basic safety controls, risk controls, authentication, auditability, and customer account visibility must remain independent of paid entitlements.

## Billing

Billing infrastructure exists, but real customer charging still requires staging verification of the selected payment provider, webhook lifecycle, idempotency, cancellation, renewal, and reconciliation. USDT/TRON payment work remains a separate change and is not assumed to be production-approved by the existence of code on an old branch.

## Release wording

Use precise terms such as paper, research, configured, server-gated, sandbox, or subject to approval unless the complete evidence chain is green.
