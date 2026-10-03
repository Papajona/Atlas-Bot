# Atlas 3.10.47 — Production Preflight Hardening

This release formalizes the remaining pre-deployment controls identified in the 3.10.46 production review.

## Security and release identity
- Release identity is now 3.10.47 / Android versionCode 1047.
- Production startup continues to require immutable release SHA, release version, and image digest.
- Withdrawal step-up tokens are capped at three minutes by application validation.
- Repository secret scanning is enforced by CI and the verification image build.

## AI/model controls
- AI trade-safety review has a 1.5-second latency ceiling and fails closed to NO_TRADE.
- Provider model IDs can be checked before deployment with scripts/check_ai_models.py.
- Live model loading and promotion remain fail-closed on artifact hash/signature verification.

## Binance transport
- Added a reconnect-safe user-stream session with explicit heartbeat/pong checks, exponential reconnect backoff, and post-reconnect reconciliation callback.

## Audit retention
- Cloud audit verification now fails if retention is below 3650 days or the sink destination does not match the configured audit bucket.

Live trading remains disabled until the external staging/testnet drills and production deployment gates are completed.
