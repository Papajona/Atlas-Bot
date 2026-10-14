# Atlas release sign-off matrix

**Purpose:** distinguish merged code, automated tests, and real environment evidence. No line may be marked PASS solely because a CI simulation completed.

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
| Original OANDA dataset identity | Original raw response, normalized CSV, separately archived reference manifest, immutable hashes and exact instrument/granularity/from/to metadata | **BLOCKED — source identity not yet established** | `scripts/evaluate_original_oanda_oos.py` confirms exact identity; a new same-length capture is insufficient |
| Chronological OOS and DSR | Full walk-forward report and complete trial ledger with every tested variant's annualized Sharpe | **BLOCKED — not evaluated against verified original data** | 5 chronological folds, leakage/purge gap, cost stress pass, DSR >= 0.95 using the complete trial ledger, plus all configured OOS gates |
| Dedicated staging database identity | Successful read-only `staging-identity-preflight.yml` run, explicit isolated staging ref vs production ref, role capability output | **BLOCKED — real staging identity not proven** | Dedicated direct login; NOINHERIT; non-owner/non-admin; explicit grants; required BYPASSRLS; RLS enabled for all public tables; revision expected |
| Backup restore | Provider restore operation ID/time and evidence from a separate staging target | **BLOCKED — CI smoke test is not a real restore drill** | Real restore completed without modifying source; constraints, revision, ledger invariants, RTO/RPO and reviewer recorded |
| Provider-to-ledger reconciliation | Sanitized sandbox/read-only provider export and exact mapping to internal order, reserve, payout, and journal records | **BLOCKED — end-to-end external evidence absent** | Every provider action reconciles exactly once; UNKNOWN payouts remain unresolved until provider truth is proven; no duplicate payout/order created |
| Signed Android artifact | Signed APK, SHA-256 file, apksigner certificate report, expected certificate fingerprint, metadata with exact final SHA and package | **BLOCKED until exact candidate artifact is independently verified** | `com.atlas.trading`, valid APK signature, expected certificate fingerprint, matching independent checksum, version and SHA recorded |
| Cloud Monitoring policies | Policies deployed for exact project/service/region and a delivered test notification | **NOT VERIFIED** | Policy IDs/config plus test delivery and on-call owner recorded |
| Python dependency lock | Committed transitive lock with artifact hashes, strict install, successful reproducibility CI | **BLOCKED — direct version pins are not a full hash-locked dependency set** | Clean build installs with hash enforcement from reviewed committed lock; no runtime dependency resolution |
| Container base image | Both runtime and verification Dockerfiles pinned to approved base-image digest | Runtime pinned; verification pin included in follow-up changes | Docker CI builds and verifies each pinned base at candidate SHA |
| Operational sign-off and branch protection | Named operator/reviewer approvals, emergency stop exercise, branch protection/ruleset evidence | **BLOCKED — settings-level branch protection has not been verified** | Required checks enforced on main and sign-offs linked to the same release identity |

## Evidence log template

Append one row for every attempt, including failures; never overwrite failed results.

| UTC time | Exact commit | Gate | Result | Evidence URL / SHA-256 | Reviewer / action |
|---|---|---|---|---|---|
| _Record on execution_ | _40-char SHA_ | _Gate name_ | PASS / FAIL / BLOCKED | _Run URL, restore ID, artifact hash, or sanitized evidence_ | _Owner and next action_ |

## Decision rule

The release is **NO-GO** while any required gate is BLOCKED, FAIL, or lacks a verifiable evidence link. A green CI run does not satisfy a staging runtime identity, original data provenance, real restore, provider reconciliation, notification delivery, branch-protection, or signed-artifact requirement.

No approval is granted here to enable live trading, real payouts, TRON mainnet funding, or sweeping. Keep the app in paper/staging until each real-world gate is independently passed and reviewed.
