# Atlas Subscription Verification Matrix

This matrix separates source implementation from server enforcement, automated tests, and operational evidence.

| Capability | Code | Server gate | Regression coverage | Staging E2E | Safe release wording |
|---|---|---|---|---|---|
| Market scanner | Yes | Yes | Partial | Required | Research/scanner |
| Smart Trade | Yes | Yes | Partial | Required | Paper planning |
| DCA bot | Yes | Yes | Yes/static | Required | Paper until live evidence |
| Grid bot | Yes | Yes | Partial | Required | Paper only |
| Alerts | Yes | Yes | Static | Required | Configuration unless evaluator/worker proven |
| Arbitrage | Yes | Yes | Yes | Required | Paper/research |
| Strategy Builder | Yes | Yes | Yes | Required | Research/validation |
| Customer webhooks | Yes | Yes | Yes/static | Required | Authenticated webhook capability |
| AI credit allowance | Yes | Yes for Strategy Builder generation | Yes | Required | Metered Strategy Builder allowance |
| Exchange limit | Yes | Yes for OANDA, Deriv, Binance provisioning | Yes | Required | Enforced connection allowance |
| Strategy count | Yes | Yes for customer bot/executor creation | Yes | Required | Enforced active automation allowance |
| Portfolio analytics | Yes | Yes | Contract coverage | Required | Server-gated analytics |
| Portfolio risk | Yes | Yes | Contract coverage | Required | Server-gated risk analytics |
| Daily research | Yes | No dedicated paid entitlement proven | Partial | Required | Do not market as exclusive |
| Advanced AI | Partial | No distinct plan gate proven | Partial | Required | Do not market as exclusive |
| Trading journal | Not sufficiently proven | No | No | Required | Do not advertise |
| API access | Not sufficiently proven | No | No | Required | Do not advertise |
| Priority support | Service capability | N/A | N/A | Human process | Requires service commitment |

## Evidence rule

A green static or contract test proves source-code structure only. It does not prove worker execution, exchange connectivity, Cloud Run deployment, customer UI behavior, or production operation. Online payment and checkout are out of scope.

## Payment scope

Online payment processing, checkout, payment-provider webhooks, recurring charges and referral commissions tied to paid invoices are not part of this release. Plan changes are operator-managed; catalog prices are not charges. Do not add payment secrets or activate paid entitlements from an external payment event without a separate approved product decision and reviewed implementation.
