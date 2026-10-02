# Security model

The application is fail-closed for live trading. Paper trading is the default, and the runtime blocks live execution unless production flags, credentials, non-sandbox mode, authentication, single-worker execution, and explicit live confirmation are all satisfied.

Exchange API keys should be restricted to trading operations only, with withdrawals disabled and IP restrictions enabled where the venue supports them. Store secrets in a managed secret store in production; do not commit `.env` files.

The dashboard uses a session-only admin token in the browser. Put the application behind HTTPS and a trusted reverse proxy or identity layer before exposing it to the internet.

A CCXT network timeout on an order is treated as an UNKNOWN outcome rather than a failed order. Operators must reconcile the broker state before retrying.

## Application-layer database encryption (3.3.4)

The hardened backend encrypts sensitive database values with authenticated Fernet encryption before they are written to the database. The ORM transparently decrypts them when the application reads them. Covered fields include customer email/display name, TRON deposit address and token contract, withdrawal destination/tag/error/rejection/signature, funding metadata, and audit details.

Set `APP_ENCRYPTION_KEY` to a generated Fernet key and store it in Google Secret Manager (or an equivalent managed secret store). Do not commit the key or place it in Android code. Existing plaintext records must be converted with `scripts/encrypt_existing_data.py` after applying Alembic migration `0008_encrypt_sensitive_data`.

The encryption key is separate from the Supabase credentials, exchange keys, TRON master seed, and withdrawal release secrets. Rotate it using a controlled migration procedure; do not simply replace it on a running database because previously encrypted records would become unreadable.

## Customer OTP step-up

Customer withdrawals now require a fresh Supabase email/SMS OTP verification and a short-lived, signed backend step-up token. The token is bound to the authenticated Supabase user and expires according to `WITHDRAWAL_STEP_UP_MINUTES` (10 minutes by default). OTP values are never stored or logged by the application. OTP endpoints are rate-limited more aggressively than normal API routes.

## Accepted dependency risk: ecdsa / PYSEC-2026-1325 (reviewed 2026)

`pip-audit` flags `ecdsa 0.19.2` for PYSEC-2026-1325 (CVE-2024-23342, "Minerva" timing
attack on P-256 ECDSA signing). This is pulled in transitively by `bip-utils` (not a
direct dependency). Upstream `python-ecdsa` has stated side-channel attacks are out of
scope for the project and **there is no planned fix for any version** -- this cannot be
resolved by upgrading.

**Reviewed and accepted, not ignored**, for these specific, verified reasons:

1. The advisory affects `ecdsa.SigningKey.sign_digest()` -- i.e. ECDSA *signing*, private
   key generation, and ECDH. It explicitly does **not** affect signature *verification*.
2. Every call site that imports `bip_utils` in this codebase was checked (`app/main.py`,
   `app/tron_sweep.py`, `app/usdt_tron.py`). All of it is **public-key-only address
   derivation and validation**: `derive_usdt_tron_address_from_xpub()` derives TRON
   deposit addresses from an account-level **extended public key**, never a seed or
   private key ("the server never receives the seed" -- see the function's own
   docstring), and `validate_tron_account_xpub()` explicitly asserts
   `ctx.IsPublicOnly()`. BIP32 public-key child derivation is a distinct elliptic-curve
   operation from `ecdsa.SigningKey.sign_digest()` and does not invoke it.
3. Actual custody signing is deliberately kept **outside this application entirely** --
   see `ExternalSignerPayoutProvider` in `payout.py` and the sweep-intent design in
   `tron_sweep.py`. This app never holds a TRON private key, so it has no private-key
   signing operation for the Minerva timing attack to target in the first place.

**Net assessment**: the vulnerable code path (`SigningKey.sign_digest()` / key
generation / ECDH) is not reachable through this application's actual usage of
`bip-utils`. This is reviewed as a low-risk, accepted, upstream-unfixable finding, to be
re-verified whenever `bip-utils`'s dependency on `ecdsa` changes, or if any future code
adds a private-key operation through this library.
