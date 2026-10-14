# Atlas-Bot audit repair activity — 2026-10-14

## Repository baseline and prior activity
- Repository: `Papajona/Atlas-Bot`; this repair branch was cut from the current main commit `218ddb4d852e4ba7490b4b4278405e91cf096723` (seen at 2026-10-13T18:13:31Z).
- The previous audit reported a syntax pass for 273 Python files, and a custom mini-pytest-shim result of 524 passed / 6 skipped / 56 failed, with 34 of 122 test files uncollectable because dependencies were unavailable. The shim results are historical and are not treated as a valid pytest run.
- Earlier work also included CI/security hardening, migration graph checks, and Android debug-build work. Prior not-yet-verified evidence included production-gate success on the final release commit, signed Android APK/certificate/checksum, backup restore, provider reconciliation drills and branch protection. These are separate from the repairs in this PR.

## Repairs on `fix/audit-repair-verified-2026-10-14`
- Training validation now aligns fold probabilities to the fixed encoded classes when a chronological training fold omits a rare label. Complete triple-barrier holding-window timeouts are labeled from terminal return with a half-volatility neutral dead zone; incomplete terminal windows remain unlabeled. Model metadata stores the actual encoded model classes and an explicit mapping.
- Non-finite order-book values, quantity, prices, fees, latency, volatility and adverse-cost budget are rejected. The research-only execution plan handler maps non-finite cost sentinels to JSON `null` instead of emitting invalid JSON.
- Correlation-group exposure falls back to average entry price if mark price is missing/zero/invalid. Zero-quantity positions contribute zero exposure. If a non-zero position cannot be valued, proposed exposure is denied.
- User-supplied text comparisons in authentication/signature paths use constant-time UTF-8 byte comparisons, preventing `compare_digest(str, str)` from raising on non-ASCII characters.
- Stage/production model loading requires a manifest signature independently of `MODEL_INTEGRITY_REQUIRED_FOR_LIVE`.
- Withdrawal execution defaults off. Destination checks fail closed when whitelisting is disabled or the allow-list is empty, including in staging.
- The quantity-reference AST fallback now includes `_QTY_QUANTUM`. New regression tests cover missing-class log loss, timeout labels, non-finite execution inputs, strict JSON output, model-signature fail-closed behavior, withdrawal allow-list behavior, UTF-8 comparisons and transient provider verification retries.
- AI defaults/examples/deployment scripts were moved from the identifiers raised by the audit to `gemini-3.6-flash` and `gpt-oss-120b`. `scripts/check_ai_models.py` retries transient HTTP 429/5xx/network errors a bounded number of times and does not convert a persistent provider failure into success.
- Android manifest configuration explicitly disables cleartext, configures system-trust TLS, keeps Android backups disabled, and supplies legacy/Android 12+ exclusions for app data. The Android debug build/security checks have passed in CI.
- The local Compose setup now mounts `/app/model-staging` as its own named volume while retaining the read-only root filesystem.
- The PostgreSQL service image in Atlas CI, Production Verification Gate and Ledger Concurrency workflow was changed from the ECR public mirror to Docker Hub after GitHub Actions logs showed the ECR pull failed before tests with `toomanyrequests: Data limit exceeded`.

## Verification evidence observed
- A real GitHub Actions full pytest run after the core repairs completed with **856 passed, 16 skipped, zero failures**. The three failures from the earlier run (zero-quantity risk handling and two stale source-text assertions for HMAC calls) were fixed and the subsequent run passed.
- The mandatory PostgreSQL ledger/concurrency workflow completed successfully on the repair branch.
- Android security/debug compilation completed successfully. The signed Android release job was skipped on pull-request context; this is not a signed release APK verification.
- The live AI provider gate still fails when Gemini responds with HTTP 503 even after the bounded retry path. Groq `openai/gpt-oss-20b` model listing and Chat Completions inference succeeded in the observed logs. `CEREBRAS_API_KEY` was not supplied to that workflow, so Cerebras inference has not been verified. **Do not waive the Gemini failure or treat the overall production gate as green.**

## Not verified / must remain blocked
- The latest commit's complete set of workflow statuses must be checked before merge; the newest workflow set has been observed running.
- A successful live Gemini inference for the configured model remains a release blocker; repeat after provider capacity recovers or select a different model only after verifying it against provider catalog and real inference with authorized credentials.
- Cerebras live inference is unverified because no key was present in CI.
- A signed Android release APK, certificate fingerprint, package ID and SHA-256 checksum have not been verified in this PR; the release job is intentionally unavailable on the pull-request branch.
- Production secrets/identity, live exchange order execution, TRON mainnet funding/sweeping, customer money/payouts, a real backup restore from the staging backup system, and live provider reconciliation were not executed by this patch.
- Docker base image digest pinning and package hash-locking were not changed because an authoritative digest/hash set was not available in this environment; do not invent hashes or digests. The exact package versions in `requirements.txt` remain pinned by version but are not all hash-locked.
- No schema changes or Alembic migration changes were made in this patch. No mainnet funding, live trading, or live payout settings were enabled by this patch.

## Release policy
This PR is not a production-release approval. Keep it unmerged until the latest required Atlas CI and PostgreSQL checks are green and the Production Verification Gate has successful provider verification. Keep paper-only/live-funding safeguards unchanged, and do not mark the Android release as signed without its signing and checksum evidence.


## Follow-up from the uploaded deep audit — 2026-10-14
- Reviewed the user-uploaded `ATLAS_AUDIT_REPORT.md`. Its explicit evidence boundary is preserved: the offline mini-pytest results do not replace a real pytest run; staging database identity, isolated Supabase migration validation, backup restore/provider reconciliation, and signed APK evidence cannot be inferred from static review.
- Updated `scripts/verify_release.sh` to use the same two pip-audit advisory exceptions as mandatory Atlas CI: `PYSEC-2026-1325` and `CVE-2026-104874`. This makes the local release script's policy consistent with CI; it does not claim either advisory is harmless beyond the existing documented exception policy.
- Added `:app:testDebugUnitTest` to the Android pull-request workflow before assembling the debug APK. This closes the workflow gap where Kotlin unit tests were not executed. The test task has not yet been proven green on the resulting commit.
- The following remain operational evidence gates, not source-only fixes: verify the deployed runtime database identity and effective grants; apply/validate migration 0049 only on an isolated staging clone and exercise real application roles; restore a backup to a clean target and reconcile provider transactions against the ledger; produce a signed release APK and verify package, signing certificate fingerprint, and SHA-256; retrain after timeout-label changes and evaluate chronologically out of sample.
- Current branch remains `fix/audit-repair-verified-2026-10-14`; do not merge until fresh checks complete on the latest head and operational gates are recorded with evidence.


## Labeling threshold clarification — 2026-10-14
The timeout dead-zone threshold has been aligned from `0.25 * v[i]` to `0.5 * v[i]`, following the explicit example in the uploaded deep audit. A new test distinguishes the two thresholds using a terminal return of +0.4% when volatility is 1%. This patch requires exact-head CI verification. Retraining and chronological out-of-sample evaluation against the original dataset remain outstanding until its instrument, granularity, fixed request window and SHA-256 provenance can be verified.


## Follow-up verification — 2026-10-14 (exact PR head review)

Repository: `Papajona/Atlas-Bot`; PR #114 remains open and unmerged.

### CI evidence inspected
- Atlas CI run `38535096347`: passed. Python job completed its full test suite with **863 passed, 16 deselected**; its separately required Postgres-marked suite reported **16 passed, 863 deselected**. The optional OANDA practice connectivity step was skipped because its credentials are optional.
- PostgreSQL ledger concurrency run `38535096171`: passed.
- Production Verification Gate run `38535096201`: passed, including disposable least-privilege runtime-role fixture and safe-mode regression checks. This is CI evidence, not proof that the connected staging deployment uses that role.
- Android release workflow run `38535096245`: overall workflow concluded success, but its `android-release` signing job was **skipped** and the run has **no artifacts**. The debug build passed; no signed APK/certificate/SHA-256 evidence exists for this run.
- CI completed the disposable PostgreSQL backup/restore smoke test, migration from the pre-lockdown fixture through public-schema lockdown, runtime lockdown checks, migration graph round-trip, and TRON/provider reconciliation regression tests. These are isolated regression tests, not a real production backup restore or live-chain/provider reconciliation drill.

### Labeling and chronological evaluation
- The timeout-label patch is present in `app/trading_core.py`: when a complete holding window ends without barrier touches, it labels by terminal return with a half-volatility dead zone; incomplete future windows remain unlabeled.
- Unit tests for timeout direction/dead-zone behavior and missing-class log-loss alignment are present and included in the passing Python suite.
- **Actual retraining and chronological out-of-sample evaluation remain blocked**: the original 1,799-row OANDA dataset and its instrument/granularity/timestamp/hash provenance are not present in the accessible repository. No trained artifact or out-of-sample performance claim is made.

### Live connected Supabase role check (read-only)
The connected project query returned `postgres` and `service_role`, but no `atlas_runtime`, `atlas_backend`, or `atlas_app` role. `postgres` owns all 57 public tables and has administrative privileges. `service_role` has `BYPASSRLS` and owns no public tables, but it is a reserved Supabase role and must not be treated as the application's dedicated runtime role. Therefore live staging identity/database-access verification is **not passed**.

### Release decision
Keep PR #114 unmerged. The remaining external prerequisites are: provision the dedicated staging runtime identity and protected staging configuration; execute the main-only staging identity preflight in the protected environment; run a successful protected Android signing job on an approved main commit and retain its APK evidence; and provide the original dataset with verifiable provenance before retraining and chronological out-of-sample evaluation. No production data was modified by this verification pass.
