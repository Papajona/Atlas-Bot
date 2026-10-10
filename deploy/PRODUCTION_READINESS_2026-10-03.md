# Production readiness — 2026-10-03

This document distinguishes source-level controls from operational evidence. A control existing in source is not proof that the production infrastructure has been configured or tested.

## Current decision

**LIVE-MONEY STATUS: NO-GO**

Do not enable customer live trading or real-money withdrawals until every Critical/High item below has current staging/production evidence.

## Corrections applied in this review

- Android release builds no longer use the debug keystore. Release signing now requires CI-provided signing variables.
- Added a gated Android workflow: every PR can build a debug APK; production-tagged builds require a protected production environment and a base64-encoded release keystore secret.
- Removed the Android biometric security bypass that previously allowed risk-control authorization on devices without supported/enrolled authentication.
- Risk dashboard HTTP telemetry is now protected by the server-side RISK_OFFICER authorization boundary.
- Risk dashboard WebSocket telemetry authenticates before accepting the socket.
- Mobile risk telemetry no longer invents healthy-looking defaults when the backend is unavailable or omits required fields; it fails closed.
- Added regression tests for the above controls.

## Remaining production evidence

### Critical

- [x] Native mobile operator login now uses the existing Supabase-backed admin login + MFA challenge/verification flow and only enables native risk controls after an AAL2 access token is returned.
- [x] Native mobile emergency halt now POSTs the risk kill endpoint with the authenticated bearer token and requires authoritative server confirmation; local UI state is not the source of truth.
- [x] A native reset/recovery action is gated by strong biometric/device-credential authorization and the authenticated server response. Reset is **not** a live-trading resume operation: the backend clears the application halt but deliberately leaves live trading disabled and Atlas in PAPER mode.
- [ ] Android release APK is built by CI from the protected production environment and verified with apksigner verify. (Debug compilation is now part of normal CI.)
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
- [ ] Physical Android device test completed for login/MFA, risk telemetry, emergency halt, biometric reset authorization, certificate/host restrictions, and release build.

## Broker and asset routing

The authoritative execution design is:

| Asset class | Venue | Current authority | Status |
|---|---|---|---|
| Crypto | Binance | Platform Binance credentials for platform trades; verified isolated Customer Binance accounts for customer trades | **Live-capable, subject to the production gates below** |
| Forex | OANDA | OANDA practice/demo path for research, backtesting and demo execution | **Live execution disabled** |
| Commodities | OANDA | OANDA practice/demo path for research, backtesting and demo execution | **Live execution disabled** |
| Deriv | Deriv | Separate Deriv adapter/preflight exists, but real-account execution is intentionally disabled and not part of the unified live execution/outbox/ledger path | **Live execution disabled** |

Atlas must not route live Forex or commodity orders through Binance. The execution code explicitly blocks OANDA live Forex/commodity execution and treats those markets as demo/paper-only. Deriv is also not currently an approved live execution venue for Atlas; its real-account buy/sell capability is explicitly disabled until it is integrated into the unified live execution controls.

### Binance fact-check

The repository contains a Binance user-stream implementation and unit tests for signed subscription message shape. That is not equivalent to demonstrating a production customer user stream. The remaining gate is an end-to-end authenticated exchange test with observed account/order events and recovery/reconciliation evidence.

## Cloud Armor fact-check

The deployment documentation requires an external HTTPS load balancer plus Cloud Armor and restricted Cloud Run ingress. Source/configuration requirements are not operational evidence. The production gate therefore remains open until the actual Google Cloud resources and traffic path are verified.

## TRON funding-path fact-check

`PAPER_TRADING=true` and `LIVE_TRADING_ENABLED=false` do not disable the separate TRON deposit/custody path. The API and worker deployment scripts now default `USDT_TRON_ENABLED=false` and explicitly disable sweeping. Enabling TRON funding requires `ALLOW_TRON_FUNDING=YES`; mainnet additionally requires `ALLOW_MAINNET_TRON_FUNDING=YES`. Do not set these approvals for a paper/staging pilot. This source-level gate does not certify custody, treasury ownership, withdrawal reconciliation, or production funding safety.

## Backup fact-check

A required backup/recovery configuration is present in the deployment hardening, but configuration is not a restore drill. Production remains NO-GO until a restore is performed and recorded.

## Release signing fact-check

A production Android artifact must never be signed by the Android debug keystore. The build now fails closed when release signing variables are missing. This proves the source-level guard; it does not prove that the production keystore, Play/App Signing configuration, artifact verification, and rotation procedure are complete.

## Rule

**No green source-level CI result can override missing operational evidence.**

The correct status remains **NO-GO for live money** until the Critical evidence is completed.


## 2026-10-03 mobile risk-control correction

The Android control client was corrected to use the existing backend administrator authentication contract instead of inventing a separate client-side Supabase SDK integration. Native login calls the administrator login endpoint, discovers the verified TOTP factor, creates a challenge, and exchanges the 6-digit authenticator code through the MFA verification endpoint. The returned bearer token is attached to risk telemetry and risk-control requests. WebSocket telemetry is also bearer-authenticated. Risk metrics no longer use the previous healthy-looking hard-coded equity/drawdown/Sharpe defaults; unavailable telemetry fails closed. CI now includes Android debug compilation plus the native mobile security regression tests.

The native emergency control is a **server-authoritative halt/reset control**, not a local resume-trading switch. The Android client must not claim that biometric success alone resumes live trading. The kill operation requests the authoritative emergency halt. The reset operation releases the application halt state but intentionally leaves live trading disabled and returns Atlas to PAPER mode; any later re-enable of live trading remains a separate privileged operation.

**Operational caveat:** source-level integration is corrected, but live-money readiness still requires an actual authenticated Android-device test against the deployed service, including AAL2 login, kill confirmation, biometric reset authorization, token expiry/re-login, and broker-side halt verification. No claim of live-money readiness is made until those tests pass.