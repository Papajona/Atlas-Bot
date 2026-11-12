# Atlas Trading OS 3.8.1 — Hosted AI Research Architecture

- A hosted AI service is used for primary research and deeper analysis.
- A separate hosted AI service is used for fast secondary market analysis.
- Both services are called independently for auditable dual analysis.
- No additional hosted-provider integration is included.
- API keys are configuration-only and must be supplied through Secret Manager/environment variables.
- AI outputs are advisory research context only; they cannot authorize orders, withdrawals, or payout release.
- Provider failures are isolated so one unavailable provider does not discard the other provider's result.
- Default hosted model identifiers were release-specific and have since been retired.
