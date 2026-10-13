# Atlas-Bot multi-market support and verification matrix

Status is based on the current code, not intended future capability. A market is not considered live-ready because its symbol is accepted by a request schema.

| Market | Intended adapter | Allowed validation path now | Live status | Evidence required before promotion |
|---|---|---|---|---|
| Forex | OANDA v20 | OANDA practice account; normalize and validate instrument against OANDA metadata | Disabled; OANDA adapter rejects non-practice configuration | Instrument preflight, practice order lifecycle, protective stop/take-profit verification, restart/reconciliation and duplicate-submit tests |
| Commodities | OANDA v20 where the account exposes the instrument | Practice-only; query instrument metadata and reject unsupported instruments | Disabled | Same OANDA tests for each exact commodity instrument; do not assume all metals/energy CFDs are available |
| Crypto | Binance customer adapter / supported exchange adapters | Paper/sandbox and adapter-specific validation | Live execution remains gated | Exact venue/market type, amount/price precision, protective orders, ledger reservation/settlement, timeout UNKNOWN reconciliation and duplicate-submit tests |
| Deriv markets | Deriv API | Account/API preflight and demo validation only until end-to-end proof exists | Not approved for customer live trading | Demo contract proposal, buy/settle/expiry lifecycle, idempotency, timeout reconciliation, risk/amount validation and ledger reconciliation |
| Indices and stocks | A compatible provider must be explicitly integrated | Unsupported unless the selected provider/adapter confirms the instrument | Not approved | Provider-specific instrument catalogue, market-hours/calendar, units/lot rules, margin, order/protection support, sandbox lifecycle and reconciliation |

## Global safety rules

- Keep `PAPER_TRADING=true`, `LIVE_TRADING_ENABLED=false`, `PAYOUT_LIVE_ENABLED=false`, and `BROKER_SANDBOX=true` for the staging pilot.
- OANDA is practice-only in this application. Do not enable OANDA live trading by changing an environment variable.
- Reject an asset/provider combination that has not been explicitly implemented and tested. Do not route an unknown asset to a default exchange.
- Do not infer indices, equities, or commodity support from a generic `symbol` field or a broad request schema.
- Never treat subscription funds as trading balance. Do not submit customer live orders until ledger reservation, provider idempotency and reconciliation have been demonstrated.
- Promotion is per provider + instrument + order type. A passing forex demo does not certify commodities, Deriv contracts, crypto derivatives, stocks or indices.

## Verification sequence

1. Resolve the database-backed duplicate-execution race in the customer bot and prove it with a concurrent PostgreSQL test.
2. Verify withdrawal idempotency, exact-once reservation/release and UNKNOWN outcome reconciliation under concurrent requests and provider timeouts.
3. Run each adapter's practice/sandbox lifecycle tests for the exact instruments intended for the pilot.
4. Re-run all required CI checks on one commit and retain logs.
5. Restore the route-module split as a separate behavior-preserving refactor with API and import tests.
6. Build the signed Android release from approved `main`; record package identity, signing certificate fingerprint and APK SHA-256.
7. Complete staging backup/restore and provider reconciliation drills.

Until these gates are evidenced, Atlas-Bot is a multi-market-capable codebase under verification, not a verified live multi-market trading service.
