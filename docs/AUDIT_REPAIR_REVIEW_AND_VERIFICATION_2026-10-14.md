# Atlas-Bot audit repair, fact-check and verification record

**Date:** 2026-10-14  
**Repository:** `Papajona/Atlas-Bot`  
**Repair branch:** `fix/audit-repair-verified-2026-10-14`  
**Scope:** user-supplied sandbox audit findings, provider defaults, CI integrity and release-gate behavior.

## Evidence standard

A patch is not called verified merely because it was committed or compiled. Verification means the relevant automated check completed on the exact commit being reviewed. Live-provider checks establish only the provider/model/key/quota conditions at the time of the request; they do not establish trading quality or production readiness. No production deployment, live trading, live payout, or mainnet funding was authorized by this repair.

## User-supplied audit: confirmed claims and current treatment

| Finding | Assessment | Patch / action | Verification status |
|---|---|---|---|
| `train_model` can crash when a validation fold omits a class passed to sklearn `log_loss` | Reproducible in the supplied audit; class alignment is a valid failure mode | `_aligned_log_loss` added to training path | The overall suite passed 857 tests on an earlier repair head, but exact-head verification must be read from the newest CI run. |
| Triple-barrier timeouts become NaN/dropped labels and flat class is nearly absent | Material modeling/data-labeling concern; labeling changes affect model behavior | Patched completed holding-window timeouts using terminal return and a half-volatility dead zone; incomplete terminal windows remain unlabeled; added regression test | **Code/test verification pending exact-head CI.** Retraining, chronological out-of-sample validation, calibration/DSR comparison, and paper-forward evidence remain open before promotion. |
| HMAC `compare_digest(str, str)` raises for non-ASCII strings | Python's compare-digest text API requires ASCII; error handling should not turn malformed user input into a 500 | Shared constant-time text helper used at the reported call sites | Targeted reproduction was reported as passing; exact-head regression status remains tied to CI. |
| Execution planner accepts NaN and may emit infinity to JSON | Non-finite numeric inputs and JSON NaN/Infinity are invalid boundary conditions | Reject non-finite price/quantity; serialize non-finite response values as null | Targeted JSON boundary reproduction reported; full endpoint/server test not claimed. |
| Correlation exposure ignores positions without mark price | Can undercount exposure if a valid entry-price fallback exists | Fall back to average entry price in correlation risk calculation | Targeted scenario reported blocked; broader portfolio-risk regression still required. |
| `test_qty_ref_idempotency.py` fallback loader omits `_QTY_QUANTUM` | Test harness defect, not a production ledger defect | Include the required constant in fallback loader | The user-supplied audit reported 6 tests passing. |
| Cerebras default `llama3.1-8b` is deprecated | Needs provider catalogue evidence; do not infer all model IDs or endpoint availability from names | Changed defaults to a supported candidate only where checked in repository | Do not claim a live Cerebras inference pass unless the current CI provider log shows it. |
| Gemini inference default | Model existence, quota, and successful inference are separate checks | `gemini-3.5-flash` returned HTTP 429 while `gemini-3.5-flash-lite` inference succeeded in the latest gate run; config, CI, Cloud Run fallback, and runbook use `gemini-3.5-flash-lite` | Production Verification Gate passed on the tested paper-trading commit with no quota waiver. Explicit environment overrides still require live verification. |
| Groq model/API behavior | Models-list HTTP 403 does not prove chat inference is broken; endpoint and operation must match intended use | Gate uses the repository checker; no claim based solely on models-list | Exact current checker logs decide pass/fail. |
| Model artifact integrity could be optional for staging/production | Hash adjacency alone is not a trusted signature boundary if the hash can be replaced with the model | Staging/production model verification now fails closed when independent artifact signature verification is not available, regardless of the old optional flag; regression coverage added | **Code/test verification pending exact-head CI.** Ensure production artifacts have a valid signature/manifest and test both valid and tampered artifacts. |
| Withdrawal configuration permissive in staging | A staging flag must not become a path to real provider payouts | Withdrawals now default disabled and destination allow-list checks fail closed when disabled or empty | **Code/test verification pending exact-head CI.** A real staging/provider payout drill remains prohibited until explicitly authorized; verify no staging configuration can reach a real payout provider. |
| Model metadata class labels disagree with encoded model classes | Could corrupt downstream interpretation if metadata is consumed as truth | Model metadata now records the actual encoded classes and explicit mapping | **Code/test verification pending exact-head CI.** Validate compatibility with every artifact consumer before promoting a newly trained model. |
| Unknown symbols share `OTHER` correlation bucket | Can understate or distort correlation grouping | Not changed in this patch set | **Open:** define conservative unknown-symbol behavior and add mixed-asset exposure tests. |
| Docker base image floating tag | Reproducibility/supply-chain concern | Dockerfile pins the observed `python:3.13-slim` SHA-256 digest; CI validates it and builds the image | Exact-head CI must pass the pin verification and container build. Dependency hashes are a separate issue. |
| Android manifest lacks extraction/network policy | Needs manifest-specific threat model and app compatibility review | Existing Android source gate remains mandatory | Android debug build passed on a prior head; source gate failed on a prior head due to a stale provider-workflow assertion. Exact-head rerun is required. |

## Changes made in this repair branch

- Training log-loss alignment helper and terminal-return/dead-zone labels for complete triple-barrier timeouts; incomplete terminal windows remain unlabeled.
- HMAC-safe UTF-8 byte comparison at reported call sites.
- Fail-closed staged/production model signature requirement; model metadata class mapping recorded explicitly.
- Withdrawals default disabled and destination allow-list enforcement fails closed when disabled or empty.
- Non-finite execution-planner input/output handling.
- Correlation exposure fallback to average entry price.
- Quantity-idempotency fallback test-loader correction.
- Python container base-image digest pin plus CI digest verification/container build.
- Gemini defaults aligned across configuration and deployment templates to the candidate that had a successful live inference probe.
- Production verification no longer waives Gemini HTTP 429/quota failure; all configured AI providers must pass live inference.
- Regression assertions updated to check fail-closed provider-gate behavior rather than requiring the old Cerebras-specific log string.
- This audit record added so findings, changes, and unclosed work remain reviewable.

## CI evidence observed during this session

For workflow runs on a repair head before the last test assertion update:
- Python suite: **857 passed, 16 skipped, 1 failed**. The single failure was a stale test assertion expecting the removed string `Cerebras .*Chat Completions inference failed`; this is a test expectation mismatch introduced by removing the provider quota waiver, not evidence of a runtime failure. The assertion was updated to verify the checker invocation and absence of a temporary waiver. A fresh exact-head run is required to close this.
- Android security source gate failed on the same stale assertion; it must be rerun after the test fix.
- Production-safe regression suite: 857 passed, 16 skipped, 1 failed on that same stale assertion; must rerun.
- PostgreSQL ledger/concurrency workflow: **passed** on the observed repair head.
- Android debug build: **passed**; Android release job was skipped, so no signed APK is proven.
- Provider verification passed on a prior run after the model/default change, but the final workflow must pass on the exact final commit with no waiver.
- Docker digest/container steps were added after earlier runs; do not claim those checks passed until a run on the exact final commit completes.

## Release and production gaps — still blocking

1. Do not merge or deploy until all mandatory workflows pass on the exact final commit.
2. Do not claim a signed Android release: release signing job was skipped and no signed APK certificate/fingerprint/checksum was verified.
3. Runtime database identity is not proven to be the intended least-privilege Cloud Run identity. Prior audit evidence reported inspection as `postgres` with `BYPASSRLS`.
4. Supabase production inspection previously reported revision `0047_strategy_outcome_lifecycle`, 57 public tables with RLS disabled, and 3 mutable-search-path functions. Migration `0049_public_schema_rls_lockdown` must be tested against a separate staging copy and actual server/client flows before rollout. No live DDL is authorized by this audit.
5. Real backup restore and provider-to-ledger reconciliation are not proven by disposable CI smoke tests.
6. Paper-only flags must remain enforced; no live trading, payout, customer funds, or mainnet custody is authorized.
7. Research dataset provenance, label leakage, chronological out-of-sample evaluation, DSR, retraining after timeout-label changes, and paper-forward evidence remain separate model-promotion gates.
8. Dependency lockfile hash verification, production artifact signature/fingerprint validation, Android network/data-extraction policy review, and unknown-symbol correlation treatment require their own focused tests.

## Previous activity retained

- Inspected user-provided audit claims and separated reproducible code defects from unverified external provider/version claims.
- Reviewed GitHub Actions job summaries and logs rather than treating workflow badges or active steps as completion.
- Identified the quota-waiver behavior and removed the exception so provider verification fails closed.
- Observed and recorded the exact failing stale assertion from CI, updated the assertion, and required a new exact-head run.
- Added digest pin verification/container build coverage and this report.
- No production deploy, live trade, payout, mainnet transfer, or production database migration was performed as part of this work.

## Final status rule

**Not release-ready until the latest exact-head Atlas CI, PostgreSQL concurrency, Android security/debug, and production verification checks all conclude success, and operational staging/restore/reconciliation/signed-APK gates are independently evidenced.**

## Labeling threshold clarification — 2026-10-14
The uploaded audit recommends a timeout flat-label dead zone such as `|terminal_return| < 0.5 × sigma`. The branch now uses `0.5 * v[i]` and has a regression case for a +0.4% timeout return with 1% volatility, which must be neutral. The matching CI run is required before the threshold implementation is called verified. Training on the original 1,799-row dataset and chronological out-of-sample model validation remain **not complete** because the dataset's original instrument, granularity, timestamp window, and matching hash manifest are not present in the accessible repository/library evidence.
