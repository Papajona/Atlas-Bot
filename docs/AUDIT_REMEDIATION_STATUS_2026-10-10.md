# Atlas-Bot Audit Reconciliation — 2026-10-10

Scope: the supplied `AUDIT_REPORT_2026-10-10.md`, the supplied “Code Fixes and Patches” proposal, the current Android remediation branch, and the production/security controls directly relevant to those findings. This is not a claim that every historical audit in `docs/history` has been re-audited line-by-line.

## Confirmed findings and branch changes

| Finding | Evidence reviewed | Branch action | Status |
|---|---|---|---|
| Development Cloud Run URL was embedded in Android main string resources | `app/src/main/res/values/strings.xml` on `main` contained the `ais-dev-...` URL | Removed backend URL and trusted-host resources; backend comes from BuildConfig | Changed in PR #102; latest build still pending |
| Release could use a missing/unapproved backend URL | Release workflow and Gradle build configuration | Release task dependencies now require HTTPS URL, a matching separately configured approved host, and all release signing variables | Implemented; must pass latest CI and verify production environment values |
| Dashboard displayed seeded customer money and trade values | Android dashboard source had a fixed balance, trade count, profit percentage, hard-coded market quotes and activity | Removed seeded values; displays unavailable until verified data exists | Changed; static regression tests added |
| Dashboard generated fake 30-day equity history | `generate30DayEquityData()` synthesized a positive curve | Removed the generator and leaves performance history unavailable | Changed; static regression test added |
| Settings used a fabricated customer identity | Settings had an Alice Henderson email default | Removed hard-coded identity/email defaults and caller-supplied fabricated email | Changed; static regression test added |
| Sensitive app screens allowed screenshots by default | `MainActivity` | Sets `FLAG_SECURE` on the app window | Changed; device-level screenshot test still required |
| Risk dashboard could show default zero metrics while telemetry was absent/stale | `RiskDashboard.kt` defaults metrics to zero and tracks telemetry freshness | Numeric risk cards now require connected authenticated WebSocket telemetry updated within 90 seconds; otherwise show unavailable warning | Changed; Android runtime behavior still requires verification |
| Strategy replay could be mistaken for verified account performance | `StrategyReplayEngine.kt` generates deterministic synthetic scenarios | Renamed and added an explicit disclaimer that it is synthetic, not verified historical backtest data or customer P&L | Changed |

## Existing controls verified in source

- `app/config.py` already rejects invalid `ENVIRONMENT` values and has a production/staging validator for several deployment and model-integrity requirements. Therefore “no startup validation exists” is too broad; additional checks may still be needed.
- The release workflow already checks the signing certificate SHA-256 against `ATLAS_RELEASE_CERT_SHA256`, checks package identity `com.atlas.trading`, and uploads the APK, signing report, checksum, and metadata after a signed release succeeds.
- Production CI already runs PostgreSQL-backed migration, security, ledger concurrency, and regression checks.
- `SECURITY.md` documents the accepted `ecdsa` / `PYSEC-2026-1325` risk. The CI dependency-audit command explicitly ignores that advisory, so the correct status is “documented accepted exception,” not “dependency vulnerability eliminated.”
- The production release workflow is configured for manual dispatch on `main` or an `android-v*` tag. A pull-request build only builds the debug APK and skips the signed-release job.

## Proposed fixes intentionally not applied as written

1. **Signature permission on the launcher activity:** not applied. The launcher activity must be launchable by the Android home/launcher; a custom signature permission would prevent the normal launcher from starting the app. Keep the launcher exported as required and protect sensitive actions through app authentication/authorization.
2. **Certificate pinning with an all-zero placeholder pin:** not applied. A placeholder is not a verified certificate/SPKI pin and can cause all production network calls to fail. Pinning requires a verified current pin, backup pin, rotation plan, and endpoint ownership confirmation.
3. **Broad exception-handler replacement:** not applied wholesale. Some handlers deliberately convert Redis/network/adapter failures into fail-closed outcomes or typed unknown outcomes. Narrowing them without preserving those semantics can weaken safety or break error recovery.
4. **The proposed production environment sample:** not copied as written. It mixed `PAPER_TRADING=false`, `BROKER_SANDBOX=false`, and enabled mainnet TRON funding while live trading/payout flags were still false. The new `.env.production.example` is explicitly fail-closed and uses placeholders only.
5. **The proposed standalone startup-validation module:** not copied verbatim. Its sample tests instantiate local `Settings` objects but the validator reads the imported global `settings`, so those tests would not validate the objects they construct. It also exits the process directly. Existing Pydantic validation is the appropriate place for carefully tested additional configuration invariants.

## Still open — do not claim production-ready

- CI must finish green on the latest PR #102 head. Earlier runs exposed Gradle Kotlin DSL compilation errors; those were corrected, but a successful build on the final head is required.
- Configure and verify the approved `ATLAS_PROD_BACKEND_URL` and matching `ATLAS_PROD_BACKEND_HOST` in the GitHub `production` environment.
- Run the signed release workflow on `main`; verify the produced APK's certificate fingerprint, package ID, checksum and uploaded artifact.
- The Android customer sign-in and real customer account-data API are not wired by this branch. “Unavailable” is safer than fake values, but it is not a completed customer experience.
- Operator session behavior across rotation and incident recovery, server-side WebSocket authorization revocation, device screenshot protection, and device/emulator smoke tests remain runtime gates.
- No live trading, live payout, or TRON funding enablement is authorized by these changes.

## Safe operational posture

Keep `PAPER_TRADING=true`, `LIVE_TRADING_ENABLED=false`, `CUSTOMER_LIVE_TRADING_ENABLED=false`, `BROKER_SANDBOX=true`, `PAYOUT_LIVE_ENABLED=false`, `DERIV_LIVE_ENABLED=false`, and `USDT_TRON_ENABLED=false` until the relevant external runtime acceptance gates are completed.
