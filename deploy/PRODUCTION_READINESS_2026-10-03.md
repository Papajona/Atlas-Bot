# Production readiness — 2026-10-03

This document distinguishes source-level controls from operational evidence. A control existing in source is not proof that the production infrastructure has been configured or tested.

## Current decision

**LIVE-MONEY STATUS: NO-GO**

Do not enable customer live trading or real-money withdrawals until every Critical/High item below has current staging/production evidence.

## Corrections applied in this review

- Android release builds no longer use the debug keystore. Release signing now requires CI-provided signing variables.
- Added a gated Android workflow: every PR can build a debug APK; production-tagged builds require a protected production environment and a base64-encoded release keystore secret.
- Removed the Android biometric security bypass that previously allowed resume on devices without supported/enrolled authentication.
- Risk dashboard HTTP telemetry is now protected by the server-side RISK_OFFICER authorization boundary.
- Risk dashboard WebSocket telemetry authenticates before accepting the socket.
- Mobile risk telemetry no longer invents healthy-looking defaults when the backend is unavailable or omits required fields; it fails closed.
- Added regression tests for the above controls.

## Remaining production evidence

### Critical

- [ ] Native mobile operator login establishes a real Supabase AAL2/RISK_OFFICER session before native risk controls are enabled.
- [ ] Native mobile emergency halt performs POST /api/risk/kill using that authenticated session and waits for server confirmation. A local UI toggle is never authoritative.
- [ ] Resume requires biometric/device credential and a fresh server-side authorization decision.
- [ ] Android release APK is built by CI from the protected production environment and verified with apksigner verify.
- [ ] Release signing key is stored outside GitHub source and outside the APK; rotation/recovery procedure is documented.
- [ ] External HTTPS load balancer and Cloud Armor are deployed and verified from the public hostname. Direct Cloud Run run.app ingress must not be the public production path.
- [ ] Backup restore drill has been executed against a disposable PostgreSQL/Supabase target, including integrity checks and recovery-time evidence.
- [ ] Worker heartbeat certification has been executed with multiple instances/restarts and stale-heartbeat detection.
- [ ] Binance customer user-stream integration has been demonstrated against the intended Binance environment, including reconnect, event processing, unknown-order handling, and reconciliation.

### High

- [ ] Production Redis distributed-rate-limit/lease failover tested.
- [ ] Cloud Run API and worker service accounts have least-privilege Secret Manager/IAM access.
- [ ] Public /healthz and /readyz checks pass through the real load-balancer path.
- [ ] Cloud Armor blocking/rate-limit rules have been exercised and observed in Cloud Logging.
- [ ] Exchange sandbox drills cover timeout, UNKNOWN order, duplicate submission, partial fill, stop rejection, cancel race, and worker restart.
- [ ] Physical Android device test completed for login/MFA, risk telemetry, kill switch, resume authorization, certificate/host restrictions, and release build.

## Binance fact-check

The repository contains a Binance user-stream implementation and unit tests for signed subscription message shape. That is not equivalent to demonstrating a production customer user stream. The remaining gate is an end-to-end authenticated exchange test with observed account/order events and recovery/reconciliation evidence.

## Cloud Armor fact-check

The deployment documentation requires an external HTTPS load balancer plus Cloud Armor and restricted Cloud Run ingress. Source/configuration requirements are not operational evidence. The production gate therefore remains open until the actual Google Cloud resources and traffic path are verified.

## Backup fact-check

A required backup/recovery configuration is present in the deployment hardening, but configuration is not a restore drill. Production remains NO-GO until a restore is performed and recorded.

## Release signing fact-check

A production Android artifact must never be signed by the Android debug keystore. The build now fails closed when release signing variables are missing. This proves the source-level guard; it does not prove that the production keystore, Play/App Signing configuration, artifact verification, and rotation procedure are complete.

## Rule

**No green source-level CI result can override missing operational evidence.**

The correct status remains **NO-GO for live money** until the Critical evidence is completed.
