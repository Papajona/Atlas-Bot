# Atlas Subscription Verification Matrix

This acceptance matrix distinguishes implemented, gated, tested, and operationally proven.

| Capability | Code | Listed | Server gate | Automated test | E2E | Release wording |
|---|---|---|---|---|---|---|
| Market scanner | Yes | Yes | Yes | Partial/static | Required | Research/scanner |
| Smart Trade | Yes | Yes | Yes | Partial/static | Required | Paper planning |
| DCA bot | Yes | Pro/Elite | Yes | Yes/static | Required | Paper until live gate proven |
| Grid bot | Yes | Yes | Yes | Partial/static | Required | Paper only |
| Alerts | Yes | Yes | Yes | Yes/static | Required | Configuration unless worker proven |
| Arbitrage | Yes | Yes | Yes | Yes | Required | Paper/research only |
| Strategy Builder | Yes | Elite | Yes | Yes | Required | Research/validation |
| Customer webhooks | Yes | Elite | Yes | Yes/static | Required | Authenticated webhook capability |
| Daily research | Yes | Starter+ | No customer plan gate proven | Yes | Required | Not exclusive paid entitlement yet |
| Advanced AI | Yes/partial | Pro+ | No dedicated gate proven | Partial | Required | Not exclusive entitlement yet |
| Risk engine | Yes | Pro | No dedicated plan gate proven | Partial | Required | Not exclusive entitlement yet |
| Portfolio analytics | Partial | Pro | No dedicated gate proven | No sufficient entitlement test | Required | Not yet paid entitlement |
| Trading journal | Not sufficiently proven | Pro/Elite | No | No | Required | Do not advertise |
| API access | Not sufficiently proven | Elite | No | No | Required | Do not advertise |
| Priority support | Service capability | Elite | N/A | N/A | Human process | Requires service commitment |
| AI credit allowance | Plan field | Yes | Usage enforcement not proven | Tier-order test only | Required | Not a hard allowance yet |
| Exchange limit | Plan field | Yes | Count enforcement not proven | Tier-order test only | Required | Not a hard limit yet |
| Strategy count limit | Plan field | Yes | Count enforcement not proven | Tier-order test only | Required | Not a hard limit yet |

## Required lifecycle tests

1. New customer gets the configured trial/free state.
2. Verified Stripe checkout changes plan only after authoritative provider event.
3. Duplicate webhook is idempotent.
4. Failed/past-due subscription loses protected entitlement according to policy.
5. Cancellation preserves service until the documented period end.
6. Expired trial falls back to Free.
7. Lower-tier customer cannot invoke a higher-tier endpoint.
8. Upgraded customer can invoke it after authoritative entitlement update.
9. Referral discount and commission remain separate from customer trading funds.
10. Billing records reconcile with the revenue ledger.

## Evidence rule

A green static test proves source-code structure only. It does not prove Stripe, notifications, exchanges, Cloud Run, Redis, or the customer UI are working in production.
