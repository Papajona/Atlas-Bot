# Atlas operations and release runbook

## Mandatory safety posture

This repository's default deployment posture is staging/paper only. Keep all of the following true for routine verification and pilot rehearsal:

- `PAPER_TRADING=true`
- `LIVE_TRADING_ENABLED=false`
- `PAYOUT_LIVE_ENABLED=false`
- `BROKER_SANDBOX=true`
- `USDT_TRON_SWEEP_ENABLED=false`

Do not place customer funds at risk while collecting release evidence. Do not enable mainnet funding, sweeping, live withdrawals, or live execution to make a check pass. A green CI job, a simulation, or a debug APK is not proof of a real deployment control.

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
  --expected-rows 1799 \
  --asset forex --folds 5 --min-train 800 \
  --output oanda-oos-evidence.json
```

The trial ledger must be JSON with `schema_version: 1`, `complete: true`, `all_variants_included: true`, a non-empty `experiment_id`, and unique `variant_id` entries each carrying a finite `annualized_sharpe`. Those flags are an auditable assertion, not a substitute for retaining the actual experiment archive. Review and archive the dataset hashes, trial-ledger hash, complete output JSON, code SHA, walk-forward fold metrics, net costs, drawdown, trades, DSR, and cost-stress results. A failed gate means no model promotion. A passed research gate does not authorize live execution.

For a repeatable protected run, configure `ATLAS_OANDA_ARCHIVE_BUCKET`, `ATLAS_ORIGINAL_OANDA_REFERENCE_OBJECT` (under `trusted-reference/`), and `ATLAS_RESEARCH_TRIAL_LEDGER_OBJECT` (under `research-trials/`) as staging environment variables. Configure a separate least-privilege `GCP_STAGING_RESEARCH_SERVICE_ACCOUNT` secret through Workload Identity Federation. Use the `Original OANDA walk-forward release evidence` workflow from `main`, supply the exact durable archive prefix `oanda-practice/{run-id}-{capture-commit-sha}`, and retain the resulting evaluation artifact and GCS report even when the research gate fails. A missing reference manifest or complete trial ledger is a hard block. The workflow will not manufacture those inputs or reinterpret a new capture as the old dataset.

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
