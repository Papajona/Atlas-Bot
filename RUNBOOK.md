# Atlas operations and release runbook

## Mandatory safety posture

This repository's default deployment posture is staging/paper only. Keep all of the following true for routine verification and pilot rehearsal:

- `PAPER_TRADING=true`
- `LIVE_TRADING_ENABLED=false`
- `PAYOUT_LIVE_ENABLED=false`
- `BROKER_SANDBOX=true`
- `USDT_TRON_SWEEP_ENABLED=false`

Do not place customer funds at risk while collecting release evidence. Do not enable mainnet funding, sweeping, live withdrawals, or live execution to make a check pass. A green CI job, a simulation, or a debug APK is not proof of a real deployment control.

## Free single-project Supabase customer-paper mode

Do not create a second Supabase project or a paid branch for the early paper-only stage. The low-cost option is a dedicated `customer_paper` schema in the existing project's `postgres` database, accessed only through a separate runtime role and credential. This is **schema/ACL isolation**, not a separate database failure domain: it shares storage, compute, availability, and backup/restore scope with the rest of the project.

This mode is opt-in and disabled by default. Configure `CUSTOMER_PAPER_SHARED_DATABASE_ENABLED=true` only after the schema and least-privilege role are provisioned and verified. `CUSTOMER_PAPER_DATABASE_URL` must use the same host, port, and `postgres` database as the primary URL but a unique paper runtime role and a different password. Keep `CUSTOMER_PAPER_LEDGER_ENABLED=false` until the preflight passes against that exact endpoint and role. Never use Supabase's `postgres`, `service_role`, `authenticated`, or `anon` identity as the paper runtime role.

### Operator setup sequence (existing project only)

1. Store the current project's administrative database URL in a protected local environment variable such as `ATLAS_ADMIN_DATABASE_URL`; do not paste its password into shell commands, commit it, or print it to logs. Confirm the URL ends in the existing `/postgres` database. Take/confirm a current recoverable backup or export before schema DDL.
2. Open an interactive `psql "$ATLAS_ADMIN_DATABASE_URL"` session. Create a dedicated runtime login, then set its password using psql's hidden password prompt (the secret is not part of the command line/history):

   ```sql
   CREATE ROLE atlas_customer_paper LOGIN NOINHERIT NOBYPASSRLS
       NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
   ```

   Then run the psql meta-command `\\password atlas_customer_paper` at the prompt and grant database connectivity:

   ```sql
   GRANT CONNECT ON DATABASE postgres TO atlas_customer_paper;
   ```

   Do not give this role membership in another role, ownership of tables, schema CREATE, UPDATE/DELETE, or any elevated attribute. If the role already exists, stop and inspect its attributes/grants instead of blindly recreating or reusing it.
3. From the reviewed Atlas-Bot checkout, run the schema file against that same existing database as the reviewed database owner (not as the paper runtime role):

   ```bash
   psql "$ATLAS_ADMIN_DATABASE_URL" -v ON_ERROR_STOP=1 \\
     -v paper_runtime_role=atlas_customer_paper \\
     -f scripts/customer_paper_ledger_schema.sql
   ```

   Review the file and the target URL before execution. This creates the `customer_paper` schema/tables and narrowly grants the runtime role. Do not run it against production just to make release checks green.
4. Store the paper URL with the same project/host/database but username `atlas_customer_paper` and its separately generated password in the approved secret store. Set `CUSTOMER_PAPER_SHARED_DATABASE_ENABLED=true` only for the preflight. Keep `CUSTOMER_PAPER_LEDGER_ENABLED=false` during this read-only check. Run:

   ```bash
   python -c "import asyncio; from app.customer_paper_ledger import preflight_isolated_customer_paper_ledger; print(asyncio.run(preflight_isolated_customer_paper_ledger(require_enabled=False)))"
   ```

   Proceed with paper-only rehearsal only if the exact endpoint reports `status=PASS`, `isolation_mode=shared_database_schema`, and the runtime role/permissions match this runbook. Any other result is a hard stop; do not broaden grants to make it pass. The preflight is read-only and does not enable paper execution.

The paper role must be `LOGIN NOINHERIT NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION`, have no role memberships, own no paper tables, have no `CREATE` privilege on any schema, and have privileges only for `SELECT`/`INSERT` on the seven paper tables plus `USAGE` on their identity sequences. It must have no privileges on any real-money table, sequence, or executable `SECURITY DEFINER` function. Forced row-level security and the exact tenant policy are mandatory. The application refuses to proceed if any of these checks fail.

Provisioning `scripts/customer_paper_ledger_schema.sql` is DDL against the existing database; it is **not** run automatically by the application. Before a human operator runs it, confirm a usable backup/export exists and review the target database and role. This change does not modify the connected Supabase database.

This mode is suitable only for early paper development/rehearsal after the schema preflight and PostgreSQL lifecycle tests pass. It does not count as an independently restored staging target or prove disaster recovery. Keep live trading, customer-fund movement, withdrawals, and payouts disabled; production clearance still requires a separately corroborated recovery drill and any applicable provider-reconciliation evidence without initiating real-money transactions.

## Emergency stop

Call authenticated `POST /api/risk/kill`. The local kill switch is set before broker cancellation is attempted. The endpoint then attempts to cancel open orders. If cancellation fails, the system remains HALTED and the broker account must be checked manually. Record the incident, release SHA, service revision, event timestamp, cancellation result, and follow-up owner.

Do not resume entries because a timeout elapsed. An operator must verify broker positions, unresolved orders, service health, and the relevant incident record first.

## Automatic execution-health guardrails

The default configuration is intentionally conservative:

| Signal | Default threshold | Required operator response |
|---|---:|---|
| Consecutive losing strategy outcomes | 3 | Stop new entries, inspect strategy outcomes and broker reconciliation |
| Recent order error rate | 5% over the configured rolling order window | Stop new entries; inspect venue health, error classes, and UNKNOWN orders |
| Recent order latency p95 | 2,000 ms | Stop new entries; verify venue latency and avoid retries that could duplicate orders |
| Recent order window | 20 orders | Insufficient observations must not be interpreted as proof that the venue is healthy |

These are fail-closed *new-entry* controls implemented in the risk gate, not a guarantee that all positions have been flattened. Reduce-only/closing actions remain distinct from new entries. When a guard trips, preserve the `EXECUTION_HEALTH` incident and logs. Never clear the incident or manually resubmit an ambiguous order before reconciliation.

## Broker timeout or UNKNOWN order

1. Do not resubmit the same logical trade or payout.
2. Preserve the client idempotency key, provider request/response IDs, timestamps, and sanitized error.
3. Call authenticated `POST /api/reconcile` for the appropriate venue/symbol, in paper/sandbox or read-only mode only.
4. Reconcile provider order/payout state against the internal order, reserve, and ledger records.
5. Keep ambiguous payouts in `UNKNOWN`; do not release reserves or issue a second provider request until an operator has established the actual provider outcome.
6. Record the evidence and operator decision in the incident/sign-off record.

## Dataset provenance and chronological out-of-sample model evaluation

A fresh capture of the same row count is **not** proof that it is the original research dataset. An accepted evaluation requires the exact original raw-response SHA-256 and normalized-CSV SHA-256, instrument, granularity, fixed `from`/`to` request parameters, first/last candle timestamps, and a separately archived trusted reference manifest.

Capture files with `scripts/capture_oanda_research_dataset.py` and verify their internal hashes with `scripts/verify_oanda_research_dataset.py`. Preserve the source response and manifest in an access-controlled, durable archive; GitHub's finite-retention workflow artifact is a transport/review copy, not a permanent archive.

The DSR calculation must use the annualized Sharpe of **every** strategy/feature/threshold/parameter variant considered during model selection. Do not substitute the number of chronological folds for the number/distribution of tested variants. Keep the complete immutable trial ledger alongside the dataset. The evaluation will stop without both inputs:

```bash
python scripts/evaluate_original_oanda_oos.py \
  --dataset-dir /secure/archive/original-oanda-dataset \
  --reference-manifest /secure/archive/original-reference-manifest.json \
  --trial-ledger /secure/archive/complete-trial-ledger.json \
  --experiment-archive /secure/archive/complete-experiment-archive.zip \
  --expected-rows 1799 \
  --asset forex --folds 5 --min-train 800 \
  --output oanda-oos-evidence.json
```

The OOS evaluator derives annualization from the trusted OANDA candle granularity (for example, D = 252 observations/year, H1 = 6,048, M5 = 72,576) and refuses unknown granularities. It also requires explicit staging variables for `ATLAS_RESEARCH_TAKER_BPS`, `ATLAS_RESEARCH_SLIPPAGE_BPS`, `ATLAS_RESEARCH_CARRY_BPS_PER_BAR`, and `ATLAS_RESEARCH_MAX_HOLD_BARS`. Populate those from the intended target venue’s actual fee schedule, evidenced slippage/market impact, product financing/swap schedule, and research horizon for the verified candle interval. Do not carry over a generic hourly default to daily candles or assume carry is zero unless the evidence supports it. The experiment ZIP’s manifest must bind those exact assumptions, together with the dataset hashes and every tested variant, so re-running with different costs or granularity cannot be mistaken for the same experiment.

The trial ledger must be JSON with `schema_version: 1`, `complete: true`, `all_variants_included: true`, a non-empty `experiment_id`, a 64-hex `experiment_archive_sha256` matching the **full experiment archive ZIP**, and unique `variant_id` entries each carrying a finite `annualized_sharpe`. The ZIP must itself contain exactly one `atlas_trial_manifest.json` whose experiment ID and complete variant/Sharpe list match the ledger and whose raw-response hash, normalized-CSV hash, instrument, granularity, and request parameters match the trusted original dataset identity. The evaluation refuses to run if the archive is absent, malformed, missing that manifest, or its SHA-256/data identity/trial list differs. Those flags are auditable assertions, not a substitute for retaining the actual source experiment logs and reviewing the archive manifest. Review and archive the dataset hashes, trial-ledger hash, complete output JSON, code SHA, walk-forward fold metrics, net costs, drawdown, trades, DSR, and cost-stress results. A failed gate means no model promotion. A passed research gate does not authorize live execution.

For a repeatable protected run, configure `ATLAS_OANDA_ARCHIVE_BUCKET`, `ATLAS_ORIGINAL_OANDA_REFERENCE_OBJECT` (under `trusted-reference/`), `ATLAS_RESEARCH_TRIAL_LEDGER_OBJECT` (under `research-trials/`), and `ATLAS_RESEARCH_EXPERIMENT_ARCHIVE_OBJECT` (under `research-experiments/`, ZIP archive) as staging environment variables. Configure a separate least-privilege `GCP_STAGING_RESEARCH_SERVICE_ACCOUNT` secret through Workload Identity Federation. Use the `Original OANDA walk-forward release evidence` workflow from `main`, supply the exact durable archive prefix `oanda-practice/{run-id}-{capture-commit-sha}`, and retain the resulting evaluation artifact and GCS report even when the research gate fails. A missing reference manifest or complete trial ledger is a hard block. The workflow will not manufacture those inputs or reinterpret a new capture as the old dataset. Configure `ATLAS_RESEARCH_CARRY_BPS_PER_BAR`, `ATLAS_RESEARCH_CARRY_EVIDENCE_SOURCE`, and `ATLAS_RESEARCH_CARRY_EVIDENCE_ID` only from a reviewed instrument/venue financing schedule. The source and ID are immutable references to the actual schedule/version/hash, not labels invented for a passing test. The evaluator binds them into the dataset/experiment assumptions; the full archived trial manifest must match these assumptions. Missing or mismatched carry evidence blocks promotion.

## Staging runtime database identity

Run `.github/workflows/staging-identity-preflight.yml` manually from `main`, using the protected `staging` environment and explicit non-production Supabase project ref. The job is read-only: it verifies the deployed Cloud Run service account, database Secret Manager reference, database host, connection identity, Alembic revision, public-table RLS, and runtime role capabilities. It never creates roles or alters grants.

A valid result requires a dedicated login role that is not a table owner, superuser, gateway/service role, or role/database administrator; has `NOINHERIT`; has explicitly granted access; and has the reviewed backend-only `BYPASSRLS` capability required by migration 0049's no-generic-policy design. **Do not assume that the currently connected Supabase project is staging until its project ref is explicitly compared with the production ref.** If the protected environment/variables or dedicated role are absent, the job must remain blocked. Do not use `postgres` or `service_role` as the deployed application's database identity.

The repository's main branch has previously been reported as lacking branch protection. A check that merely requires `refs/heads/main` is not the same as verified branch protection. Configure and verify required status checks in repository settings with an account that has permission to do so.

## Backup restoration and ledger/provider reconciliation drill

Run this only against a separately isolated staging target. Restore into a *new or disposable target*, never over the source database. Do not relax the current Supabase plan, create a paid branch, or substitute a production connection to work around missing isolation.

1. Record backup provider/source, backup ID or timestamp, source project ref, intended staging project ref, database revision, and the person authorizing the drill. Verify the source and target project refs differ before restoring.
2. Restore the backup using the provider-supported restore path. Record the restore operation ID and completion time. Do not report the CI disposable-database smoke test as this drill.
3. Run `python -m alembic check`, `deploy/validate-postgres-constraints.py`, the PostgreSQL ledger concurrency tests, and the targeted ledger/withdrawal recovery tests against the restored target.
4. Compare journal debits and credits, journal-to-entry relationships, withdrawal/payout reserves, idempotency keys, provider IDs, and unresolved `UNKNOWN` outcomes. Store sanitized queries/results and the row counts/hash of the exported evidence, without database URLs, credentials, or customer PII.
5. Reconcile against the provider's sandbox/practice or read-only transaction export. Every provider operation must map to exactly one intended ledger transition. Do not issue a real transfer or retry a payout as part of verification.
6. Record discrepancies, fixes, rerun results, restore RTO, data-loss/RPO observations, and reviewer sign-off. A test suite running on a temporary database is supporting evidence only; it is not a backup restoration or external-provider reconciliation.

For the read-only database checks, first perform the provider's real restore into a dedicated staging project and retain its operation record. Then run `Audit restored staging database` manually from `main` with the provider operation ID, immutable backup reference, and operation-record URL/URI. The workflow checks that the target host maps to the explicit staging project (distinct from production), that the database revision is exactly `0051_research_trial_archive`, and that public-table RLS, role permissions, journal balance/idempotency, and non-negative customer ledger buckets pass. Its supplied operation ID/URI is operator-provided evidence metadata; a reviewer must corroborate it against the backup provider's own record. The workflow is read-only and does not perform the restore or modify/migrate the database.

For payout statement matching, upload a provider-generated, appropriately filtered CSV to the restricted `ATLAS_PROVIDER_STATEMENT_BUCKET` under `provider-statements/`. Required columns are `provider_id,amount,currency,status,event_time_utc`; every row must refer to an Atlas withdrawal payout, not unrelated venue/account activity. Then run `Reconcile provider payout statement to staging ledger` manually from `main`. Configure the protected staging variables `ATLAS_PROVIDER_STATEMENT_BUCKET`, `ATLAS_STAGING_EVIDENCE_BUCKET`, `ATLAS_STAGING_SUPABASE_PROJECT_REF`, `ATLAS_PRODUCTION_SUPABASE_PROJECT_REF`, `ATLAS_STAGING_DATABASE_SECRET`, and `ATLAS_STAGING_RUNTIME_DB_ROLE`, plus Workload Identity Federation and the dedicated `GCP_STAGING_RECONCILIATION_SERVICE_ACCOUNT`. The job compares provider IDs, exact amounts/currencies, normalized provider outcomes, reserve/settlement/release journal idempotency, journal balance, RLS state, and runtime database privileges using a read-only connection. The statement must cover every withdrawal created within the chosen internal timestamp window; unmatched IDs or UNKNOWN operations without provider IDs fail the gate. The detailed evidence is stored in the restricted GCS evidence bucket; the Actions summary contains counts and the statement hash only.

If no isolated target or read-only provider proof is available, mark both drills BLOCKED and keep the pilot closed.

## Alerting

Deploy the Cloud Monitoring policy set with `bash deploy/gcp-alert-policies.sh` in the intended project, using a valid notification channel. The script checks metric descriptor availability and the request-count label/latency units before applying policies; if the project's metric schema differs, update and re-review the filters rather than bypassing that check. Inspect the resulting policies in Cloud Monitoring and send a test notification. Script execution alone is not proof that notification delivery works. Keep the policies tied to the exact Cloud Run service and region; do not silently deploy global/noisy policies.

At minimum, monitor sustained Cloud Run 5xx responses, p95 request latency, and high CPU/memory utilization. Add a verified uptime check for the public health endpoint and route it to the same on-call notification channel. Document owner, routing, escalation, and acknowledgement expectations.

## Signed Android release

A release candidate must be built from the approved `main` commit, not an earlier successful workflow run or PR debug artifact. The Android release workflow must verify:

- APK package `com.atlas.trading`;
- `apksigner verify --verbose --print-certs` succeeds;
- certificate SHA-256 matches `ATLAS_RELEASE_CERT_SHA256`;
- APK SHA-256 checksum and metadata record the exact `GITHUB_SHA`, version, package, and certificate;
- Kotlin unit tests and the release build pass.

Archive the signed APK, checksum, signing report, metadata, workflow URL, and exact commit together. Verify the downloaded artifact checksum independently before distribution. If the release job is skipped or its signing secrets are absent, there is no signed-release evidence.

## Restart after outage

1. Keep live trading and payouts disabled.
2. Start the application and verify `/readyz`.
3. Reconcile all open and UNKNOWN orders/payouts.
4. Verify broker positions and account equity against a timestamped source.
5. Review audit/security logs, kill-switch state, and execution-health incidents.
6. Confirm database identity, backup availability, alert notifications, model manifest signature, and app checksum.
7. Obtain the release sign-off matrix approvals before considering any live gate. Never change funding/execution flags as a shortcut through a failed check.

## Model incident

Disable live trading, preserve the model artifact and metadata, keep its manifest/signature and SHA-256, run the original-dataset provenance check and chronological walk-forward backtest, inspect feature/data quality and the full trial ledger, then retrain and promote only an artifact whose exact evidence passes the configured gates.

## Release evidence retention

Keep a versioned release folder containing `SIGNOFF_MATRIX.md`, the exact Git commit and workflow URLs, staging identity output, dataset/experiment manifests, DSR/OOS report, restore and provider-reconciliation evidence, Cloud Monitoring policy/notification test evidence, and signed APK evidence. Redact tokens, secret values, database URLs, customer identifiers, and private key material before archiving.
