# Atlas Strategy Benchmark Data Collection

## Scope

Atlas collects a strategy-builder prompt only when the customer explicitly checks the per-request benchmark consent box. Both the quick Native AI strategy builder and Strategy Lab default to unchecked. Existing strategy requests, other API requests, and historical prompts are not backfilled.

The capture contains the submitted prompt, the resulting Atlas Native AI strategy/output, market fields (asset, symbol, exchange, timeframe), an integrity hash, the consent version/time, and the Atlas app version. It does not record IP address, user agent, email, or display name in the capture table. The customer ID is retained so the customer can view the count and delete their submissions.

## Consent and minimisation

The opt-in text states the purpose and the 90-day retention period and asks the customer to confirm that the prompt contains no personal, account, contact, credential, wallet-key, or other sensitive information. Obvious email-address strings and credential-related phrases are rejected as an additional guard. This guard is deliberately not described as complete PII detection: customers must still remove sensitive content before opting in.

Consent is recorded per submission with consent version `atlas-strategy-benchmark-v1` and a timestamp. Not checking the box means no capture. Capture failure (including missing encryption configuration) does not block strategy generation and returns a non-stored status.

## Storage, access and deletion

Prompt and Native AI output are encrypted through Atlas's existing application Fernet keyring before insertion. There is no plaintext fallback. The table has RLS enabled and grants are revoked from `PUBLIC`, `anon`, `authenticated`, and `service_role`; access is through the backend database connection only. Captures are capped at 20 per customer per 24 hours, with concurrent requests serialized per customer on PostgreSQL.

Each capture receives an `expires_at` timestamp 90 days after creation. A monitored application-side retention loop checks for expired captures hourly and physically deletes them when the connected database role has the required `DELETE` privilege. The loop deliberately disables itself for read-only roles; it does not widen the staging paper-reader role. Migration 0062 does not install or depend on `pg_cron`. Physical deletion therefore occurs on the next successful hourly sweep (or an earlier status request), rather than at the exact expiry instant. The customer can also delete all their captures immediately using the authenticated `DELETE /api/customer/benchmark-captures` endpoint or the dashboard control; that endpoint requires AAL2.

## Benchmark claims

This collection provides consented prompt/Native AI output pairs for future annotation and evaluation. It does not itself create human reference labels or paired competitor outputs, and it does not claim a real-customer quality benchmark has been completed. A benchmark report must wait for human-labelled cases and, for system comparison, paired outputs from a named comparator on the exact same prompt set.
