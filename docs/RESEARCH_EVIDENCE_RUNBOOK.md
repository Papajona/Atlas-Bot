# Atlas research evidence and customer-paper ledger runbook

This runbook distinguishes code validation from the external evidence that code must verify. A signed JSON document is not evidence merely because it has the right shape: its signature must match a key configured in `RESEARCH_EVIDENCE_PUBLIC_KEYS_JSON`, and a reviewer must independently check that the source artifacts actually support its claims.

## Research API evidence inputs

The authenticated `POST /api/research/backtest-all`, `POST /api/research/paper-signals`, and `POST /api/research/review` endpoints accept optional, size-bounded documents in their request body:

- `point_in_time_universe_manifest` (up to 5 MiB)
- `execution_cost_calibration_evidence` (up to 512 KiB)
- `trial_performance_archive` (up to 4 MiB)

The API attaches these documents to the loaded dataframe, but never trusts them just because the caller submitted them. Research reports stay blocked unless signatures, trust roots, venue/market/symbol context, source hashes, date coverage, and each validator's additional rules all pass. They cannot change the loader's data-provenance fields. Do not send signing private keys through the API, environment variables, logs, or repository.

Trusted-key configuration maps each non-empty key ID to a Base64-encoded raw 32-byte Ed25519 public key. The signer signs the canonical JSON bytes of the whole document *excluding* its `attestation` property; canonical JSON uses sorted keys, separators `,` and `:` without spaces, UTF-8, and disallows NaN/Infinity. The attestation contains `algorithm: "Ed25519"`, `key_id`, and `signature_base64`. Keep private signing keys outside Atlas and govern changes to the public trust root through a separate reviewed configuration process.

## 1. Historical point-in-time crypto universe

Required evidence: exchange-specific history covering the full backtest period and showing instruments when they were actually tradable, including instruments later delisted or failed. A current exchange market list or a current-coin allowlist is not sufficient.

The signed document must use `evidence_type: "crypto_point_in_time_universe"`, match the exact `venue` and `market_type` loaded from market data, cover the entire dataset date range, and set `membership_policy: "POINT_IN_TIME"`, `coverage_complete: true`, `includes_delisted: true`, and `includes_failed_assets: true`. Its source list must include `listing_history`, `delisting_history`, and `failed_assets`, each with an independently retrievable source URI and SHA-256 of the raw source file. Each instrument requires a unique symbol, timezone-aware `listed_at`, and optional timezone-aware `delisted_at`. `instruments_sha256` is the canonical SHA-256 of the instrument rows sorted by symbol (case-insensitive). The trusted signer must attest that missing, failed, delisted, renamed, and otherwise non-surviving assets are represented according to the source records, not merely sign a hand-picked allowlist.

The validator confirms signatures, coverage, context, row consistency and hashes; it cannot independently download or certify the upstream sources. A reviewer must retrieve each source, recompute its hash, reconcile the instrument list and dates, and record the review artifact. Until that independent source reconciliation exists, crypto-universe validation remains unresolved.

## 2. Venue-calibrated execution costs

Required evidence: actual venue-specific fee schedules, order-book/spread observations, and observed order/fill records from an appropriate non-testnet execution history. No new live order should be placed just to obtain evidence. Use existing authorized records, or keep this gate closed.

The signed document must use `evidence_type: "venue_execution_cost_calibration"` and bind to the exact venue, market type, symbol and `execution_cost_model_fingerprint` returned in the research report. Set `synthetic_only: false` and `testnet_only: false`; include independently retrievable and hashed source records of types `observed_fills`, `order_book`, and `fee_schedule`; include at least 30 observed orders, 30 observed fills, and 30 held-out validation samples; and provide timezone-aware validation start/end timestamps with `heldout_validation.status: "PASS"`.

Two additional server-side limits must be set from a reviewed, venue/product-specific calibration policy: `RESEARCH_COST_MAX_SPREAD_MAE_BPS` and `RESEARCH_COST_MAX_SLIPPAGE_MAE_BPS`. These settings intentionally have no permissive defaults. The held-out mean-absolute spread and slippage errors must be finite, non-negative, and no greater than those configured bounds. The signer must include only real, independently reconcilable data and must not infer fills or slippage from OHLCV candles alone.

Missing thresholds, testnet/synthetic evidence, mismatched model fingerprints, insufficient samples, untrusted keys, or errors exceeding the configured bounds keep calibration unverified. Setting a fee estimate, stress multiplier, or error tolerance after seeing the result to make a strategy pass is not acceptable calibration.

## 3. Comparable trial-performance archive

Required evidence: an externally reviewed, signed archive of *all* relevant historical trials, including failed and discarded configurations, with results recomputed under the same evaluation protocol and exact held-out sample as the candidate.

The document must use `evidence_type: "comparable_trial_performance_archive"`, set `archive_complete: true` and `includes_all_trials: true`, and match the report's venue, market type, asset class, symbol, timeframe, dataset SHA-256, OOS sample SHA-256 and protocol version. It needs at least two uniquely labelled trial rows and a matching `trial_count`. Every row requires its trial label, config/code/dataset/OOS/results SHA-256 values, protocol version, and annualized Sharpe; all rows must share the same held-out sample and protocol and include all current trial labels. A trusted signature attests the archive, but an auditor must still reconcile it to experiment logs, code/config snapshots and raw data.

Atlas now appends prospective attempts to `research_trial_archive` when daily or API research runs execute. Missing context or metrics are retained as unverified; the database's prospective records do not make a historical archive complete. Existing labels or counts alone cannot be converted into comparable historical performance. If past configs or OOS outputs were not retained, rerun them from reproducible historical inputs where possible and mark unrecoverable attempts as missing.

## 4. Isolated customer paper ledger

The schema, append-only journal functions, database-identity/privilege/RLS preflight, and disposable PostgreSQL tests exist in the draft PR. This is not yet authorization to enable customer paper execution: the code path must be integrated through customer paper orders, positions, fills, fees, reversals, idempotency and restart recovery, and then pass a dedicated staging isolation drill.

Provision a **separate PostgreSQL database** and a dedicated non-superuser runtime role with no memberships, no role/database creation or replication privileges, no RLS bypass, and a distinct credential. Apply `scripts/customer_paper_ledger_schema.sql` as an approved database owner using `-v paper_runtime_role=<existing-role>`. Do not use the real-money database URL as `CUSTOMER_PAPER_DATABASE_URL`. Keep `CUSTOMER_PAPER_LEDGER_ENABLED=false` until integration and evidence are approved.

Run the isolated-ledger tests with disposable PostgreSQL databases configured as `ATLAS_PAPER_TEST_DATABASE_URL` and `ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL` (CI provisions these). A preflight pass proves the checked DB identities, table grants and tenant policies at the time of that run; it does not prove the complete product flow has no alternate path into real balances. Staging sign-off requires recorded credentials/role inspection, negative attempts against real tables, paper order/fill/retry/crash-recovery tests, and before/after reconciliation demonstrating zero change to real ledger entries and balances.

## Promotion/release decision

All four gates must be backed by reviewable source artifacts and passing tests on the exact commit. The signed docs, key IDs, raw source hashes, model fingerprints, trial archive IDs and PostgreSQL preflight/reconciliation outputs should be kept as release artifacts with immutable checksums. Test fixtures, CI synthetic values, prospective trial rows, or a green code suite are not substitutes. Keep customer paper execution disabled and all live trading, payouts, withdrawals and customer-fund movement disabled until separate release evidence and approvals are complete.
