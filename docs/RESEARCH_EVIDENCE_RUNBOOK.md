# Atlas research evidence and customer-paper ledger runbook

This runbook distinguishes code validation from the external evidence that code must verify. A signed JSON document is not evidence merely because it has the right shape: its signature must match a key configured in `RESEARCH_EVIDENCE_PUBLIC_KEYS_JSON`, and a reviewer must independently check that the source artifacts actually support its claims.

## Research API evidence inputs

The authenticated `POST /api/research/backtest-all`, `POST /api/research/paper-signals`, and `POST /api/research/review` endpoints accept optional, size-bounded documents in their request body:

- `point_in_time_universe_manifest` (up to 5 MiB)
- `execution_cost_calibration_evidence` (up to 512 KiB)
- `trial_performance_archive` (up to 4 MiB)

The API attaches these documents to the loaded dataframe, but never trusts them just because the caller submitted them. Research reports stay blocked unless signatures, trust roots, venue/market/symbol context, source hashes, date coverage, and each validator's additional rules all pass. They cannot change the loader's data-provenance fields. Do not send signing private keys through the API, environment variables, logs, or repository.

Trusted-key configuration maps each non-empty key ID to a Base64-encoded raw 32-byte Ed25519 public key. The signer signs the canonical JSON bytes of the whole document *excluding* its `attestation` property; canonical JSON uses sorted keys, separators `,` and `:` without spaces, UTF-8, and disallows NaN/Infinity. The attestation contains `algorithm: "Ed25519"`, `key_id`, and `signature_base64`. Keep private signing keys outside Atlas and govern changes to the public trust root through a separate reviewed configuration process.

## 1. Historical point-in-time crypto universe

Required evidence: exchange-specific history covering the full backtest period and showing instruments when they were actually tradable, including instruments later delisted or failed. A current exchange market list or a current-coin allowlist is not sufficient.

The signed document must use `evidence_type: "crypto_point_in_time_universe"`, match the exact `venue` and `market_type` loaded from market data, cover the entire dataset date range, and set `membership_policy: "POINT_IN_TIME"`, `coverage_complete: true`, `includes_delisted: true`, and `includes_failed_assets: true`. Its source list must include `listing_history`, `delisting_history`, and `failed_assets`, each with an independently retrievable source URI and SHA-256 of the raw source file. Each instrument requires a unique symbol, timezone-aware `listed_at`, optional timezone-aware `delisted_at`, and an explicit `failed_at` field (a timezone-aware timestamp or `null` when no failure is recorded). A top-level `includes_failed_assets: true` claim cannot substitute for per-instrument failure timelines. Failure timestamps before listing are invalid; instruments whose delisting or failure predates the requested coverage window do not count as members of that window. `instruments_sha256` is the canonical SHA-256 of the instrument rows sorted by symbol (case-insensitive), including `failed_at`. The trusted signer must attest that missing, failed, delisted, renamed, and otherwise non-surviving assets are represented according to the source records, not merely sign a hand-picked allowlist.

The validator confirms signatures, coverage, context, row consistency and hashes; it cannot independently download or certify the upstream sources. A reviewer must retrieve each source, recompute its hash, reconcile the instrument list and dates, and record the review artifact. Until that independent source reconciliation exists, crypto-universe validation remains unresolved.

## 2. Venue-calibrated execution costs

Required evidence: actual venue-specific fee schedules, order-book/spread observations, and observed order/fill records from an appropriate non-testnet execution history. No new live order should be placed just to obtain evidence. Use existing authorized records, or keep this gate closed.

The signed document must use `evidence_type: "venue_execution_cost_calibration"` and bind to the exact venue, market type, symbol and `execution_cost_model_fingerprint` returned in the research report. Set `synthetic_only: false` and `testnet_only: false`; include independently retrievable and hashed source records of types `observed_fills`, `order_book`, and `fee_schedule`; include at least 30 observed orders, 30 observed fills, and 30 held-out validation samples; and provide timezone-aware validation start/end timestamps with `heldout_validation.status: "PASS"`.

Two additional server-side limits must be set from a reviewed, venue/product-specific calibration policy: `RESEARCH_COST_MAX_SPREAD_MAE_BPS` and `RESEARCH_COST_MAX_SLIPPAGE_MAE_BPS`. These settings intentionally have no permissive defaults. The held-out mean-absolute spread and slippage errors must be finite, non-negative, and no greater than those configured bounds. The signer must include only real, independently reconcilable data and must not infer fills or slippage from OHLCV candles alone.

Missing thresholds, testnet/synthetic evidence, mismatched model fingerprints, insufficient samples, untrusted keys, or errors exceeding the configured bounds keep calibration unverified. Setting a fee estimate, stress multiplier, or error tolerance after seeing the result to make a strategy pass is not acceptable calibration.

## 3. Comparable trial-performance archive

Required evidence: an externally reviewed, signed archive of *all* relevant historical trials, including failed and discarded configurations, with results recomputed under the same evaluation protocol and exact held-out sample as the candidate.

The document must use `evidence_type: "comparable_trial_performance_archive"`, set `archive_complete: true` and `includes_all_trials: true`, and match the report's venue, market type, asset class, symbol, timeframe, dataset SHA-256, OOS sample SHA-256 and protocol version. It needs at least two uniquely labelled trial rows and a matching `trial_count`. Every row requires its trial label, config/code/dataset/OOS/results SHA-256 values, protocol version, and annualized Sharpe; all rows must share the same held-out sample and protocol and include all current trial labels. A trusted signature attests the archive, but an auditor must still reconcile it to experiment logs, code/config snapshots and raw data.

Atlas now appends prospective attempts to `research_trial_archive` when daily or API research runs execute. Missing context or metrics are retained as unverified; the database's prospective records do not make a historical archive complete. Existing labels or counts alone cannot be converted into comparable historical performance. If past configs or OOS outputs were not retained, rerun them from reproducible historical inputs where possible and mark unrecoverable attempts as missing.

## 4. Isolated customer paper ledger

The schema, append-only journal functions, database-identity/privilege/RLS preflight, and disposable PostgreSQL tests exist in the draft PR. This is not yet authorization to enable customer paper execution: the code path must be integrated through customer paper orders, positions, fills, fees, reversals, idempotency and restart recovery, and then pass a dedicated staging isolation drill.

Provision a **separate PostgreSQL database** and a dedicated non-superuser runtime role with no memberships, no role/database creation or replication privileges, no RLS bypass, and a distinct credential. Apply `scripts/customer_paper_ledger_schema.sql` as an approved database owner using `-v paper_runtime_role=<existing-role>`. Do not use the real-money database URL as `CUSTOMER_PAPER_DATABASE_URL`. Keep `CUSTOMER_PAPER_LEDGER_ENABLED=false` until integration and evidence are approved.

Run the isolated-ledger tests with disposable PostgreSQL databases configured as `ATLAS_PAPER_TEST_DATABASE_URL` and `ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL` (CI provisions these). A preflight pass verifies the checked DB identities, absence of application-schema/table/sequence access to the primary DB, paper table grants and tenant policies at that time; it does not prove the complete product flow has no alternate path into real balances. Staging sign-off requires recorded credentials/role inspection, negative attempts against real tables, paper order/fill/retry/crash-recovery tests, and before/after reconciliation demonstrating zero change to real ledger entries and balances.


## 5. Candidate-selection enforcement

The paper-signals candidate path re-verifies the original signed universe, execution-cost, and comparable-trial documents using the configured public-key trust root. A report flag, validation summary, evidence ID, or previously computed `VERIFIED` label by itself is not accepted. The raw documents must still match the current dataset hash, venue, market type, symbol, timeframe, execution-model fingerprint, OOS sample hash, protocol version, and all trial labels. Crypto candidates additionally need the full-run coverage window and a point-in-time universe document. The execution-cost archive must pass the configured server-side held-out error bounds. A tampered or mismatched document returns no candidates.

The verification test `test_paper_candidates_reverify_original_signed_evidence` uses generated keys and synthetic source files solely to test acceptance/rejection logic. It is not external market evidence and must not be reused for deployment.

## 6. Staging restore and customer-paper isolation proof

The manual workflow `.github/workflows/staging-restore-audit.yml` runs only on `main` in the protected `staging` GitHub environment. It requires separate configured production/staging refs and retrieves two different GCP Secret Manager values:
- `ATLAS_STAGING_DATABASE_SECRET`: the restored application staging database URL.
- `ATLAS_STAGING_CUSTOMER_PAPER_DATABASE_SECRET`: the separately provisioned customer-paper PostgreSQL database URL.

Both must resolve to the intended isolated staging environment; the customer-paper preflight checks the database name/identity, non-overlapping runtime role and credentials, RLS/tenant policies, no UPDATE/DELETE rights on paper-ledger tables, and no access to primary real-money tables/sequences. A missing secret, absent schema, wrong role, wrong project/database identity, or unsafe grants fails closed. A preflight PASS establishes database/schema/ACL invariants only; it does not replace order-to-journal integration, crash/retry recovery, tenant-negative tests, or before/after reconciliation against the real-money ledger.

### Provider restore evidence format

`RESTORE_EVIDENCE_URI` must identify the actual provider operation record in JSON (an HTTPS endpoint that returns the provider JSON, or a `gs://` object). The downloaded record must identify all of:
- operation ID (`restore_operation_id`, `restoreOperationId`, `operation_id`, `operationId`, or `id`);
- the immutable source backup reference (`backup_reference`, `backupReference`, `source_backup_id`, `sourceBackupId`, `backup_id`, or `backupId`);
- target staging Supabase ref (`target_supabase_project_ref`, `target_project_ref`, `targetProjectRef`, or `restored_project_ref`);
- successful terminal status (`COMPLETE`, `COMPLETED`, `SUCCEEDED`, `SUCCESS`, `DONE`, or `RESTORED`, case-insensitive).

The workflow downloads that record, validates the exact operation/source/target/status, calculates its SHA-256 and passes the verified digest and target into `scripts/verify_staging_restore.py`. That audit also checks that `paper_order_states`, `paper_bar_states`, `paper_fill_events`, and `research_trial_archive` exist; a database with a manually changed Alembic revision but missing objects cannot pass. Provider JSON, verification output, database invariant report, and customer-paper preflight output are archived as workflow artifacts and to the configured evidence bucket when configured. A provider webpage that returns HTML, a free-form operator statement, or a CI-generated record is not provider-native restore proof.

Do not invoke the workflow until the protected staging environment has the correct secrets/variables and an actual restore operation has been completed. This repository change does not create the missing staging database, backup/restore operation, GCP secret values, or real provider evidence. Customer paper trading remains disabled until the real staging preflight and the separate operational isolation drill have both passed.

## Promotion/release decision

All four gates must be backed by reviewable source artifacts and passing tests on the exact commit. The signed docs, key IDs, raw source hashes, model fingerprints, trial archive IDs and PostgreSQL preflight/reconciliation outputs should be kept as release artifacts with immutable checksums. Test fixtures, CI synthetic values, prospective trial rows, or a green code suite are not substitutes. Keep customer paper execution disabled and all live trading, payouts, withdrawals and customer-fund movement disabled until separate release evidence and approvals are complete.

## 7. Reproducible local evidence-bundle verification

Before a promotion review, retrieve the raw upstream artifacts independently into a restricted local directory and map each exact URI in the signed evidence to a relative filename. Do not download arbitrary URIs from inside the verification script; it intentionally performs no network requests.

The bundle contains:
- **context.json:** exact venue, market type, asset class, symbol, timeframe, full coverage window, dataset/OOS SHA-256, protocol version, execution_cost_model_fingerprint, and the complete current candidate_labels list;
- **universe.json, calibration.json, trial_archive.json:** the actual signed documents, not templates or synthetic fixtures;
- **source-map.json:** a JSON object mapping each exact signed sources[].uri to a relative path under the local raw-source directory;
- the raw source files themselves.

The point-in-time universe must map listing_history, delisting_history, and failed_assets artifacts. Cost calibration must map observed_fills, order_book, and fee_schedule. The trial archive audit additionally requires raw experiment_log, config_snapshot, code_snapshot, dataset, and oos_results artifacts so the completeness claim can be reconciled to underlying records. Every file is byte-hashed; absolute paths, parent traversal, symlink escapes, unused map entries, missing files, and hash mismatches fail closed.

Set RESEARCH_EVIDENCE_PUBLIC_KEYS_JSON to the reviewed Ed25519 public-key map and set the policy bounds RESEARCH_COST_MAX_SPREAD_MAE_BPS and RESEARCH_COST_MAX_SLIPPAGE_MAE_BPS from reviewed configuration. Then run this command from the repository root:

    python scripts/verify_research_evidence_bundle.py --context context.json --universe universe.json --calibration calibration.json --trial-archive trial_archive.json --source-root raw_sources --source-map source-map.json --output research-evidence-verification.json

A TECHNICAL_VALIDATION_PASS_PENDING_EXTERNAL_REVIEW result means only that the document signatures, context, configured error thresholds, and hashes of the supplied raw files passed. It does not assert that source coverage is complete or truthful and never authorizes promotion. Preserve the report and have an independent reviewer reconcile historical membership, observed fills/order-book snapshots, fee schedules, and all trial logs/configs/results. Never replace missing upstream records with generated fixtures or synthetic observations.

