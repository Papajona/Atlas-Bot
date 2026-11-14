# Atlas-Bot Readiness Review — 14 November 2026

**Repository:** `Papajona/Atlas-Bot`  
**Source baseline:** protected `main`, commit `fd5ba96967fc4e7f94aa9ff5aedb2e567e48b3cc`  
**Application version:** `3.10.47`  
**Decision:** live money **NO-GO** · customer-facing paper **NOT READY** · internal paper drills **CONDITIONAL GO** · current-head paper-only staging **REVERIFY REQUIRED**

This review does not supersede `deploy/SIGNOFF_MATRIX.md`. A code change or green CI result is not operational evidence unless recorded against the exact deployed commit.

## 1. Executive findings

Source inspection at the baseline SHA confirms seven probe/observability/security-gate defects addressed by the accompanying change:

1. The main application's `/readyz` body could report `ready: false` while HTTP remained 200. The canary script uses `curl --fail`, so it relies on status codes.
2. HTTP request metrics used raw URL paths as Prometheus labels, allowing unbounded label cardinality.
3. Main-app `/readyz` recomputed database, migration, Redis and admin-role checks on every request, including concurrent probes.
4. Client `X-Request-ID` values were not bounded or validated before being echoed and logged.
5. Public `/healthz` included worker error detail and timestamps.
6. Bandit was configured with `-lll` (high severity only); the paper-staging primary-reader query triggered a medium B608 finding.
7. The Python CI and Android jobs used `ubuntu-latest`, while Production Core was pinned to `ubuntu-24.04`.

The patch makes failed main-app readiness non-2xx in staging/production, adds a short **per-process** single-flight cache, bounds metrics/request identifiers, reduces public health detail, removes the SQL f-string construction, raises Bandit to medium severity and pins the CI runners.

**Staging evidence correction:** `deploy/SIGNOFF_MATRIX.md` records identity/readiness PASS on `445e9d1fa44ca1b7c88feb4f82da85ab315645d9`. That is historical PASS evidence for that exact SHA only. Current `main` is `fd5ba96967fc4e7f94aa9ff5aedb2e567e48b3cc`; therefore this review does **not** carry the older staging PASS forward. The patched commit must be deployed to a no-traffic staging revision and its identity/authenticated readiness preflight rerun before staging is marked GO for that commit.

## 2. Scope and verification limits

| Item | Evidence / limitation |
|---|---|
| Repository state | Read from GitHub at the exact baseline SHA stated above |
| Files checked | Configuration, main health/readiness routes, telemetry middleware, paper-staging DB preflight, CI and Production Core workflows, release verifier, conftest, sign-off matrix, canary/deploy scripts |
| Local test totals in the submitted review | The submission reports 2,364 passed / 33 skipped / 4 failed before its changes and 2,375 passed / 33 skipped / 4 failed after them. These totals were not independently reproduced against the GitHub checkout in this review. |
| PostgreSQL/browser/cloud/Android | Not treated as verified by the submitted local run; required exact-commit CI and environment evidence remain authoritative |
| Whole application | This is a targeted readiness/security review, not a line-by-line audit of every application module |

The uploaded source ZIP does not carry Git history. The GitHub source SHA above, not the ZIP's apparent timestamp or local version label, is the baseline for this patch.

## 3. Readiness scorecard

| Domain | Assessment | Gate |
|---|---|---|
| Configuration safety/defaults | Strong source controls; live trading, customer-live and payouts remain off by default | Preserve safe flags |
| CI/test design/supply chain | Hash-locked dependencies and mandatory PostgreSQL/no-skip workflow are present | All required checks must pass on exact PR head |
| Main app probes/telemetry | Seven issues addressed by this patch; runtime still needs verification | Deploy and probe exact patched SHA |
| Paper execution fidelity | **Gap.** The standalone calibrated estimator is used in research-cost code, but not by active platform/customer paper fill paths | Customer-facing paper blocked |
| Research evidence | **Gap.** Original OANDA dataset identity, complete-trial DSR, survivorship and carry evidence remain blocked in the sign-off matrix | No performance claims |
| Paper-only staging identity | Prior PASS only for `445e9d1...`; current candidate not yet reverified | Re-run exact-SHA preflight |
| Backup/restore | No real restore drill recorded | Production blocked |
| Alert delivery | Delivered test notification not recorded | Production blocked |
| Network perimeter | HTTPS load balancer/Cloud Armor evidence not recorded | Production blocked |
| Execution resilience | Exchange-sandbox, worker-kill and Redis failover drills not recorded | Production blocked |
| Android | Signed production artifact and physical-device evidence not verified | Required only if mobile control is in scope |
| Maintainability | Large application modules and readiness documentation split across files | Non-blocking cleanup |

## 4. Findings register

| ID | Severity | Finding | Disposition |
|---|---|---|---|
| F-01 | High | Main `/readyz` could return HTTP 200 with `ready=false`. | Fixed in patch: return HTTP 503 for failed staging/production readiness. Development/test retains its previous status behavior. |
| F-02 | High | Raw request paths created unbounded metric label values. | Fixed in patch: use matched route template; use `unmatched` when no route matched; map unknown methods to `OTHER`. |
| F-03 | Medium | Main `/readyz` caused repeated dependency checks. | Fixed in patch: a five-second, per-process single-flight cache; TTL zero disables it. This is not a distributed cache and each instance still performs its own check per TTL window. |
| F-04 | Medium | Caller-supplied request IDs were echoed/logged without bounds. | Fixed in patch: allow only 8–64 ASCII letters, digits, dot, underscore or hyphen; otherwise generate a UUID. Cap logged path length at 256 characters. |
| F-05 | Low | Public health response exposed worker error types/timestamps. | Fixed in patch: expose coarse worker status/restart count only. Keep release version, SHA and image digest because the release verifier consumes these fields. |
| F-06 | Low | Bandit ran at high severity only; paper-staging SQL used an f-string with a module-owned predicate. | Fixed in patch: use a static `string.Template` with only the module-owned predicate substituted, and run Bandit at `-ll` in CI, Production Core and `verify_release.sh`. |
| F-07 | Low | CI runners were not consistent with Production Core. | Fixed in patch: pin Python and Android jobs to `ubuntu-24.04`. |
| F-08 | High for paper validity | Calibrated paper estimator is not called by active platform/customer paper order-fill paths. | **Open.** Do not represent paper results as validation-grade. |
| F-09 | High for performance claims | Original OANDA data identity, complete-trial DSR, survivorship and carry evidence remain unresolved. | **Open.** Follow the sign-off matrix. |
| F-10 | Medium | Large modules and router concentration. | **Open**, non-blocking for this patch. |
| F-11 | Medium | Training/backtest API-tier capacity needs runtime/load verification. | **Verify** on the intended deployment before moving work to a queue/worker. |
| F-12 | Critical for live money | Operational, reconciliation, governance and recovery evidence remains incomplete. | **Open. Live-money NO-GO.** |
| F-13 | Low | Readiness documentation has different dates/statuses. | **Open.** Keep the sign-off matrix authoritative and update with exact-SHA evidence only. |
| F-14 | Low | Single-host Compose fallback resource/backup posture needs explicit treatment. | **Verify** the Compose file; do not use it as the production deployment path without controls. |

### Important scope distinction

The paper-staging ASGI app already has a separate authenticated/rate-limited/cached readiness route. The probe changes in this patch apply to the main application and do not replace paper-staging's own authenticated preflight.

## 5. Patch and rollout

The patch changes:

- `app/main.py`: wraps existing readiness computation in a short cache, returns HTTP 503 for failed readiness in staging/production, and trims worker details in public `/healthz`.
- `app/monitoring.py`: validates request IDs, uses route-template metric labels, bounds method labels and caps the logged path.
- `app/config.py`: adds readiness settings and finite/non-negative TTL validation.
- `app/paper_staging.py`: replaces the SQL f-string with a static template and module-owned predicate.
- `.github/workflows/ci.yml`, `.github/workflows/production-verification.yml`, `scripts/verify_release.sh`: runs Bandit at medium severity; pins the CI Python/Android runners.
- `.env.example`, `tests/conftest.py`, `tests/test_ops_hardening_2026_11_14.py`: documents settings and adds regression coverage.
- This review document: corrects the staging SHA, status count and verification limits.

Rollout:

1. Deploy the exact patched commit to a tagged/no-traffic staging revision.
2. Run the staging identity and authenticated readiness preflight against that exact SHA.
3. Confirm `/readyz` returns 2xx only when its readiness payload is true; failed readiness must be HTTP 503 in staging/production.
4. Use `/readyz` as readiness/startup probe and `/healthz` as liveness. Do not use dependency readiness as liveness.
5. Update dashboards/alerts that selected raw URL paths; the path label now represents a route template.
6. Emergency compatibility switches are `READYZ_FAIL_WHEN_NOT_READY=false` and `READYZ_CACHE_SECONDS=0`. Do not use the status-code compatibility switch to claim a broken revision is ready.

## 6. Paper fill-model gap (F-08)

The codebase contains `app/paper_execution_model.py`; the source review and existing readiness documentation indicate that it is not wired into the active platform or customer paper fill paths. `app/research_execution_costs.py` uses the estimator for research-cost analysis, which is not equivalent to active-order fill integration.

Safe integration requires, at minimum:

1. Build market context server-side from the same validated completed candles used by the simulator (tradable volume, spread, recent volatility and bar duration); never accept cost inputs from clients.
2. Preserve current order idempotency and append-only customer ledger semantics.
3. Persist fill-model decisions and the calibration-evidence SHA-256 with the existing order idempotency key.
4. Derive stochastic latency/rejection choices deterministically from that key so restarts replay consistently.
5. Test participation caps, residual cancellation, restart replay and cost parity.
6. Run the mandatory PostgreSQL ledger/concurrency suites with no skips.

Calibration for the currently approved posture must use archived venue evidence and/or exchange sandbox/testnet observations. **No real-money fills are proposed or authorized by this review.** Customer-facing paper remains NOT READY until active-path integration, calibration provenance and exact-SHA PostgreSQL verification are complete. Paper reports must not be presented as validation-grade before then.

## 7. Stage gates

### Internal paper drills — CONDITIONAL GO

Only with paper-only flags (`PAPER_TRADING=true`, `LIVE_TRADING_ENABLED=false`, `CUSTOMER_LIVE_TRADING_ENABLED=false`, `USDT_TRON_ENABLED=false`), virtual balances, non-validation reporting, an exercised kill switch and logged drill findings.

### Customer-facing paper — NOT READY

Do not enable `CUSTOMER_PAPER_LEDGER_ENABLED` until F-08 is closed, isolation and zero-change-to-real-ledger checks pass, authenticated readiness is green on the exact deployed SHA, disclosures/support wording are approved and performance claims remain disabled.

### Paper-only staging — REVERIFY REQUIRED FOR CURRENT HEAD

The sign-off matrix records the prior PASS on `445e9d1fa44ca1b7c88feb4f82da85ab315645d9`. Current main's baseline is `fd5ba96967fc4e7f94aa9ff5aedb2e567e48b3cc`. The earlier PASS is not proof for current main or this patch. Keep restore, provider reconciliation, alert delivery, base-image digest and other open rows blocked until their respective evidence is attached.

### Production with live money — NO-GO

The sign-off matrix requires exact-SHA evidence for the external HTTPS load balancer/Cloud Armor and public-host checks, least-privilege IAM/Secret Manager, a real restore with RTO/RPO, delivered alert notification, exchange sandbox failure-mode drills, worker-kill recovery, multi-instance heartbeat, Redis failover, provider-to-ledger reconciliation, withdrawal controls, original-data/DSR evidence, branch protection, named sign-offs, emergency stop, legal review, and signed Android/device evidence where mobile risk control is in scope.

Live trading, live payouts, TRON mainnet funding and sweeping remain disabled. This review is not approval to enable them.

## 8. Verification status

The submitted review reports 11 added regression tests, a local result of 2,375 passed / 33 skipped / 4 failed after changes, pyflakes clean and Bandit clean at medium severity. Those are **reported local results**, not independently reproduced exact-SHA CI evidence. The amended regression file adds direct validation for the readiness TTL and corrects the `/healthz` test target; the exact PR checks remain authoritative.

No claim is made here that the patched commit has been deployed, that Cloud Run has been checked, or that the open operational gates have passed.
