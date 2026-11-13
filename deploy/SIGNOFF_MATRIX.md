# Atlas release sign-off matrix

**Purpose:** distinguish merged code, automated tests, and real environment evidence. No line may be marked PASS solely because a CI simulation completed.



## Current paper-staging evidence — 2026-11-12

**Exact verified main SHA:** `445e9d1fa44ca1b7c88feb4f82da85ab315645d9`

| Check | Result | Evidence |
|---|---|---|
| Atlas CI | PASS | [run 43257475829](https://github.com/Papajona/Atlas-Bot/actions/runs/43257475829) |
| Production Core Verification (offline Native AI) | PASS | [run 43257475756](https://github.com/Papajona/Atlas-Bot/actions/runs/43257475756) |
| Mandatory PostgreSQL ledger concurrency | PASS | [run 43257475702](https://github.com/Papajona/Atlas-Bot/actions/runs/43257475702) |
| Paper-only Cloud Run staging deployment | PASS | [run 43258473229](https://github.com/Papajona/Atlas-Bot/actions/runs/43258473229) |
| Staging identity and authenticated `/readyz` preflight | PASS | [run 43259066958](https://github.com/Papajona/Atlas-Bot/actions/runs/43259066958) |

The dedicated staging database identity gate is now **PASS for paper-only staging**. The exact-main preflight verified the deployed Cloud Run runtime identity and paper-only settings, distinct primary and paper database roles/credentials within the configured same-project Supabase target, the primary `atlas_paper_reader` as read-only / non-owner / non-admin / NOINHERIT / BYPASSRLS, and the authenticated isolated-ledger `/readyz` preflight. This same-project design is intentional and documented in `RUNBOOK.md`; a second Supabase project or paid branch is not required for this staging gate.

The Production Core workflow is pinned to `ubuntu-24.04` in `.github/workflows/production-verification.yml`; the exact-main Production Core run above passed. This removes the Ubuntu runner-migration warning from the production-safety job.

These passes clear the **paper-staging identity/readiness blocker** only. They do not clear the separate original-market-data/DSR, full backup-restore, provider-to-ledger reconciliation, signed production Android artifact, monitoring-notification delivery, complete hash-locked dependency, or full production operational sign-off gates below. Live trading, live payouts, and mainnet funding remain disabled.

## Release identity

| Field | Value |
|---|---|
| Repository | `Papajona/Atlas-Bot` |
| Baseline merged PR | [#114](https://github.com/Papajona/Atlas-Bot/pull/114) |
| Baseline merge commit | `55b054009c012a58917bcedd9fc87191b927cdc7` |
| Baseline source head | `626bdbcf32c31080ca78c27af3c2f8c1208c4a31` |
| Intended runtime posture | Paper/staging only |
| Live trading / payouts / mainnet sweeping | Disabled; no approval granted by this matrix |

For a release candidate, replace the baseline commit with the **exact final commit** and append CI, staging, data, restore, provider, alerting, and Android evidence URLs for that SHA.

## Gates

| Gate | Required evidence | Status at matrix creation | Pass condition |
|---|---|---|---|
| Core audit code regression | CI run attached to exact candidate SHA; test counts and logs | Baseline PR #114 workflows reported success | All required jobs green on the final candidate SHA |
| Original OANDA dataset identity | Original raw response, normalized CSV, separately archived reference manifest, immutable hashes and exact instrument/granularity/from/to metadata | **BLOCKED — source identity not yet established** | `scripts/evaluate_original_oanda_oos.py` confirms exact identity; trusted original-data hashes match; trial ledger is bound to the full archived experiment ZIP; a new same-length capture is insufficient |
| Chronological OOS and DSR | Full walk-forward report, complete trial ledger with every variant's annualized Sharpe, and full experiment ZIP whose embedded `atlas_trial_manifest.json` matches the ledger and exact dataset identity | **BLOCKED — not evaluated against verified original data** | 5 complete chronological purged folds; annualization derived from verified candle granularity; target-venue fees/slippage/carry and label horizon explicitly evidenced and bound to the archived experiment manifest; cost stress pass; DSR >= 0.95 using the complete trial ledger; all configured OOS gates |
| Paper-staging runtime identity and readiness | Exact-main deployment plus read-only staging identity preflight, separate primary/paper DB roles and credentials, primary-role capability output, authenticated `/readyz` | **PASS — paper-only same-project staging verified on `445e9d1fa44ca1b7c88feb4f82da85ab315645d9`** | Protected Cloud Run paper-only service; `atlas_paper_reader` confirmed read-only / NOINHERIT / non-owner / non-admin; `atlas_customer_paper` isolated ledger role; distinct secrets/credentials; full authenticated ledger readiness preflight. This is not proof of a second Supabase project or production-live isolation. |
| Backup restore | Provider restore operation ID/time and evidence from a separate staging target | **BLOCKED — CI smoke test is not a real restore drill** | Real restore completed without modifying source; constraints, revision, ledger invariants, RTO/RPO and reviewer recorded |
| Provider-to-ledger reconciliation | Provider export CSV hash and protected `provider-ledger-reconciliation.yml` run against isolated staging | **BLOCKED — authoritative provider export and reconciliation evidence not yet available** | Exact provider ID/amount/currency/status match, one reserve journal, correct terminal settlement/release, balanced lines, no unmatched or unresolved payouts |
| Signed Android artifact | Signed APK, SHA-256 file, apksigner certificate report, expected certificate fingerprint, metadata with exact final SHA and package | **BLOCKED until exact candidate artifact is independently verified** | `com.atlas.trading`, valid APK signature, expected certificate fingerprint, matching independent checksum, version and SHA recorded |
| Cloud Monitoring policies | Policies deployed for exact project/service/region and a delivered test notification | **NOT VERIFIED** | Policy IDs/config plus test delivery and on-call owner recorded |
| Python dependency lock | Committed transitive lock with artifact hashes, strict install, successful reproducibility CI | **PASS — `requirements.lock` and `requirements-dev.lock` contain transitive SHA-256 hashes; exact-main Atlas CI and Production Core passed hash-enforced installs, and Production Core passed `pip_audit`** | Clean build installs with hash enforcement from reviewed committed lock; no runtime dependency resolution |
| Container base image | Both runtime and verification Dockerfiles pinned to the same approved base-image digest, with both images built in CI | **IN PROGRESS — CI now checks the shared digest and builds both images; exact-head check pending** | Successful exact-commit digest pull plus successful runtime and verification Docker image builds |
| Operational sign-off and branch protection | Named operator/reviewer approvals, emergency stop exercise, branch protection/ruleset evidence | **BLOCKED — settings-level branch protection has not been verified** | Required checks enforced on main and sign-offs linked to the same release identity |

## Evidence log template

Append one row for every attempt, including failures; never overwrite failed results.

| UTC time | Exact commit | Gate | Result | Evidence URL / SHA-256 | Reviewer / action |
|---|---|---|---|---|---|
| _Record on execution_ | _40-char SHA_ | _Gate name_ | PASS / FAIL / BLOCKED | _Run URL, restore ID, artifact hash, or sanitized evidence_ | _Owner and next action_ |

## Decision rule

The release is **NO-GO** while any required gate is BLOCKED, FAIL, or lacks a verifiable evidence link. A green CI run does not satisfy a staging runtime identity, original data provenance, real restore, provider reconciliation, notification delivery, branch-protection, or signed-artifact requirement.

No approval is granted here to enable live trading, real payouts, TRON mainnet funding, or sweeping. Keep the app in paper/staging until each real-world gate is independently passed and reviewed.
