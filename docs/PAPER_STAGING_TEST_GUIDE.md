# Atlas Paper Staging — Tester Guide

## Scope and safety

This guide is for the restricted Atlas paper-only Cloud Run service. It uses the isolated customer_paper ledger and public market candles. It does not call an exchange order API, create deposits or withdrawals, or enable customer live trading.

Use synthetic test identities and test data only. Do not enter production customer credentials, exchange API keys, wallet seed phrases, or real-money instructions.

The browser console is served at / by app.paper_staging:app. It is included in PR #474 and becomes available on the staging URL only after the change is merged to main and the main-only paper-staging deployment completes. A green PR check alone does not deploy it.

## Customer access

1. Open the paper-staging URL and confirm the header says PAPER ONLY. The page must show live trading and live payouts as disabled.
2. Create a synthetic test account with an email address you control, then complete any Supabase Auth email confirmation.
3. Sign in and enroll a TOTP authenticator (Google Authenticator or another compatible TOTP application).
4. Verify the six-digit code. The console must not show the customer workspace before the server confirms AAL2.
5. If the profile-provisioning endpoint returns a legacy identity-review conflict, stop. Use an already-approved synthetic staging tenant or complete the separate manual identity-review process. Do not relax the profile or MFA gate.

## Paper workflow

1. Initialize the virtual paper account. The response must identify the allocation as virtual and must state that no real funds were used.
2. Run the read-only market scanner for one to eight symbols. Confirm it displays the 15-minute indicators and that a failed symbol is shown as unavailable rather than being presented as a signal.
3. Submit a small BTC/USDT or ETH/USDT paper order. The browser sends only order intent; it cannot provide a fill price.
4. Confirm the order appears in the open-order view and the recent-order history. Confirm the filled quantity starts at zero.
5. Advance the order only after a later verified completed bar is available. The server enforces bar provenance, fill latency, volume capacity, fees, and the paper risk gate.
6. Refresh the portfolio. Confirm the cash balance, open positions, fill quantity, average entry price, cost basis, and realized P&L update consistently with the paper ledger.
7. Cancel a second open order and confirm its status appears in history. Confirm a terminal order no longer appears in the open-order list but remains in recent history.
8. Sign out. Confirm that paper data and order controls are hidden and that a new authenticated AAL2 session is required.

## Browser and API checks

The browser workflow in tests/browser/admin-dashboard.spec.cjs covers the paper-staging page with mocked Auth and ledger data, including readiness blocking, signup/sign-in, TOTP, scanner, malicious text rendering, order lifecycle, mobile overflow, Content Security Policy, and forbidden live-money routes.

The focused Python tests are:

~~~bash
pytest -q tests/test_paper_staging_ui.py tests/test_customer_paper_api.py
~~~

The complete paper PostgreSQL acceptance job must also pass. It provisions disposable databases in CI and exercises customer isolation, order/fill lifecycle, shared paper-bar capacity, journal balance, and restart recovery. Do not substitute SQLite-only success for the required PostgreSQL job.

## Android test build

Pull-request Android jobs publish an emulator/debug APK and adjacent SHA-256 file when the Android debug job succeeds. Download both files from the same workflow artifact, then run:

~~~bash
sha256sum -c app-debug.apk.sha256
~~~

A release-signing job is not run for ordinary pull-request builds. The CI-generated debug APK is for emulator/device testing only, not production distribution.

## Stop conditions

Stop testing and record the request ID if any of the following occurs: the paper preflight returns BLOCKED, the account is not AAL2, the identity-review gate rejects provisioning, the market-data provenance check fails, a paper journal does not balance, a tenant can read another tenant's rows, or any response suggests a live broker/exchange or real-money write occurred.

Never bypass a gate to make the console appear ready. The main-only staging deploy, identity preflight, customer-paper acceptance drill, and production release gates remain separate controls.
