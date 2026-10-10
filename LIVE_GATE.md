# Live-trading gate

The application should not be treated as live-ready merely because the executable path exists. Before mainnet trading, verify all of the following in the target deployment:

- All automated tests pass.
- Sandbox/testnet execution and reconciliation have been exercised.
- PostgreSQL and backups are configured for production.
- Secrets are externalized; exchange keys cannot withdraw funds.
- TLS and an authenticated reverse proxy are in front of the dashboard/API.
- The live deployment has one execution worker/replica.
- Model walk-forward results are independently reviewed; no profitability is assumed.
- Exchange symbol precision/minimums are verified.
- Attached stop-loss support is verified for the exact symbol.
- Kill switch and broker cancellation are exercised.
- A timeout/UNKNOWN order scenario is exercised.
- Operator runbooks are tested.

Only after these checks should production flags be changed. Starting with sandbox/testnet is strongly recommended by CCXT's own manual, and sandbox keys are distinct from mainnet keys. citeturn754682search3

## 3.4 additional gate
Production payout execution remains disabled until Redis-backed distributed controls, backup/recovery validation, staging/testnet payout reconciliation, and the 3.4 acceptance checklist have passed.

## Funding is a separate gate

Paper trading is not equivalent to a no-funds environment. The API and worker deployment scripts default the TRON funding listener off and disable sweeping. Keep it off in staging/paper exercises. Enabling TRON funding requires explicit operator approval; mainnet additionally requires a separate explicit confirmation. No deposit, sweep, or payout test should be run against mainnet as part of a paper-trading readiness check.

- Latest follow-up hardening: deployment independently verifies the downloaded APK bytes using Android SDK `apksigner` and `aapt`, compares the actual certificate SHA-256 and package identity against the protected expected fingerprint and all bundled metadata/report values, and fails closed on any mismatch. It selects a successful signed-release job for the exact deployment SHA rather than trusting a successful debug-only workflow run.
- This code change does not produce a production-signed artifact or resolve infrastructure prerequisites. The protected release run, isolated staging migration test, live database-role verification, operational backup/restore drill, and provider-backed reconciliation evidence remain pending until their protected environments and approvals are available.

## Supabase staging identity preflight

The protected, manual `staging-identity-preflight.yml` workflow verifies the explicitly configured Supabase staging project reference, reads its PostgreSQL URL from Google Cloud Secret Manager without printing credentials or placing the URI in process arguments, checks the actual connection role and Alembic revision, and inspects the RLS state. It is read-only and deliberately does not run migrations. Configure the GitHub `staging` environment variables `ATLAS_STAGING_SUPABASE_PROJECT_REF`, `ATLAS_STAGING_DATABASE_SECRET`, and `ATLAS_STAGING_RUNTIME_DB_ROLE`, plus `GCP_STAGING_PREFLIGHT_SERVICE_ACCOUNT` and the protected Google Cloud workload identity/project secrets, before dispatching it.

The connected management Supabase project is not assumed to be staging. No live database writes or migrations were performed. Staging migration, backup/recovery, and provider reconciliation remain blocked until isolated staging is identified and the relevant operational tests pass.
