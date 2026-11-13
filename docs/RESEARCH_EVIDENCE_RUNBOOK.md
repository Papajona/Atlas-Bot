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

The signed document must use `evidence_type: "crypto_point_in_time_universe"`, match the exact `venue` and `market_type` loaded from market data, cover the entire dataset date range, and set `membership_policy: "POINT_IN_TIME"`, `coverage_complete: true`, `includes_delisted: true`, and `includes_failed_assets: true`. Its source list must include `listing_history`, `delisting_history`, and `failed_assets`, each with an independently retrievable source URI and SHA-256 of the raw source file. Each instrument requires a unique symbol, timezone-aware `listed_at`, optional timezone-aware `delisted_at`, and an explicit `failed_at` field (a timezone-aware timestamp or `null` when no failure is recorded). A top-level `includes_failed_assets: true` claim cannot substitute for per-instrument failure timelines. Failure timestamps before listing are invalid; instruments whose delisting or failure predates the requested coverage window do not count as members of that window. `instruments_sha256` is the canonical SHA-256 of the instrument rows sorted by symbol (case-insensitive), including `failed_at`. The trusted signer must attest that missing, failed, delisted, renamed, and otherwise non-surviving assets are represented according to the source records, not merely sign a hand-picked allowlist.

The validator confirms signatures, coverage, context, row consistency and hashes; it cannot independently download or certify the upstream sources. A reviewer must retrieve each source, recompute its hash, reconcile the instrument list and dates, and record the review artifact. Until that independent source reconciliation exists, crypto-universe validation remains unresolved.

## 2. Venue-calibrated execution costs

Required evidence: actual venue-specific fee schedules, order-book/spread observations, and observed order/fill records from an appropriate non-testnet execution history. No new live order should be placed just to obtain evidence. Use existing authorized records, or keep this gate closed.

The signed document must use `evidence_type: "venue_execution_cost_calibration"` and bind to the exact venue, market type, symbol and `execution_cost_model_fingerprint` returned in the research report. Set `synthetic_only: false` and `testnet_only: false`; include independently retrievable and hashed source records of types `observed_fills`, `order_book`, and `fee_schedule`; include at least 30 observed orders, 30 observed fills, and 30 held-out validation samples; and provide timezone-aware validation start/end timestamps with `heldout_validation.status: "PASS"`.

Two additional server-side limits must be set from a reviewed, venue/product-specific calibration policy: `RESEARCH_COST_MAX_SPREAD_MAE_BPS` and `RESEARCH_COST_MAX_SLIPPAGE_MAE_BPS`. These settings intentionally have no permissive defaults. The held-out mean-absolute spread and slippage errors must be finite, non-negative, and no greater than those configured bounds. The signer must include only real, independently reconcilable data and must not infer fills or slippage from OHLCV candles alone.

Missing thresholds, testnet/synthetic evidence, mismatched model fingerprints, insufficient samples, untrusted keys, or errors exceeding the configured bounds keep calibration unverified. Setting a fee estimate, stress multiplier, or error tolerance after seeing the result to make a strategy pass is not acceptable calibration.

### Per-bar execution context for research returns

For research and strategy-router returns to use the same execution-cost inputs as the calibrated paper estimator, the signed venue calibration document must additionally contain a `per_bar_execution_context` object:

- `schema: "atlas-per-bar-execution-context-v1"`
- `dataset_sha256`: the exact hash in the loader's immutable data provenance;
- `bar_index_sha256`: canonical SHA-256 of the ordered UTC ISO-8601 bar timestamps;
- `spread_source_sha256`: the SHA-256 of the single `order_book` source record;
- `spread_bps`: one observed full bid/ask spread value for each data bar, in exact index order;
- `spread_observation_count`: exactly the number of bars;
- `spread_values_sha256`: canonical SHA-256 of the submitted spread list.
- `fee_source` and `fee_evidence_id`: exact matches for `RESEARCH_FEE_SOURCE` and `RESEARCH_FEE_EVIDENCE_ID`;
- `taker_fee_bps`: the exact configured `RESEARCH_TAKER_BPS` rate used in the research returns;
- `fee_schedule_source_sha256`: the hash of the single `fee_schedule` source record in the signed source list.

The configured taker fee is not accepted merely because the evidence document contains a fee-schedule URI. Atlas checks that the signed fee rate matches the configured numeric rate, that the configured source/evidence identifiers match, and that the fee-schedule hash binds to exactly one hashed fee-source artifact. The fee-source settings and rate are included in the execution-model fingerprint.

The signed context must cover the exact dataset and bar index; reordering, truncating, resampling, or substituting bars invalidates it. Volume and volatility are not accepted from the request or the evidence author: Atlas derives the median base-asset volume and realized close-return volatility from the preceding 20 completed CCXT spot bars only. This deliberately excludes derivatives, FX, commodities, non-USD/stable quote pairs, and feeds whose volume units are not verified.

The research simulator uses the same shared cost kernel as the paper estimator for half-spread, square-root volatility/participation impact, latency impact, adverse side, and per-side taker fee. Latency uses the shared deterministic SHA-256 draw and calibrated quantile interpolation. `RESEARCH_ORDER_NOTIONAL_USD` is required so the research exposure can be converted to a base-unit order quantity; there is no default. A candidate event whose modeled participation exceeds the calibrated ceiling is not represented as a successful fill: the candidate falls back to diagnostic-only costs, reports the rejection reason, and cannot set `execution_cost_model_complete=true`. Non-zero simulated rejection probability is also rejected until the research position-state simulator explicitly models rejected orders. These are intentional fail-closed limits, not reasons to relax the evidence gate.

The exact execution-model fingerprint binds the per-bar context to the paper-model coefficients, paper calibration evidence ID, research taker fee, fixed research order notional, and cost-stress multiplier. It is returned as `execution_cost_model_fingerprint` with the per-bar context hash. Static-bps or session-only fallback returns remain diagnostic and are never labelled calibrated. Even with per-bar costs verified, the separate OHLC intrabar event-order limitation remains: this work does not make OHLC bars equivalent to tick-level execution evidence.

## 3. Comparable trial-performance archive

Required evidence: an externally reviewed, signed archive of *all* relevant historical trials, including failed and discarded configurations, with results recomputed under the same evaluation protocol and exact held-out sample as the candidate.

The document must use `evidence_type: "comparable_trial_performance_archive"`, set `archive_complete: true` and `includes_all_trials: true`, and match the report's venue, market type, asset class, symbol, timeframe, dataset SHA-256, OOS sample SHA-256 and protocol version. It needs at least two uniquely labelled trial rows and a matching `trial_count`. Every row requires its trial label, config/code/dataset/OOS/results SHA-256 values, protocol version, and annualized Sharpe; all rows must share the same held-out sample and protocol and include all current trial labels. A trusted signature attests the archive, but an auditor must still reconcile it to experiment logs, code/config snapshots and raw data.

Atlas now appends prospective attempts to `research_trial_archive` when daily or API research runs execute. Missing context or metrics are retained as unverified; the database's prospective records do not make a historical archive complete. Existing labels or counts alone cannot be converted into comparable historical performance. If past configs or OOS outputs were not retained, rerun them from reproducible historical inputs where possible and mark unrecoverable attempts as missing.

## 4. Isolated customer paper ledger

The customer-paper path is implemented in `app/customer_paper_api.py`, `app/customer_paper_flow.py`, and `app/customer_paper_lifecycle.py`. The paper-only Cloud Run entrypoint mounts these routes only, requires an existing active customer plus AAL2/MFA, and checks a read-only primary identity, a separately credentialed paper runtime role, tenant RLS, and the paper/live/payout kill switches before each operation. The current staging topology intentionally uses the existing Supabase project and a dedicated `customer_paper` schema; the primary API credential is read-only and the paper credential has no privileges on real-money tables. Do not replace this with a shared `DATABASE_URL` credential.

CI now runs `scripts/customer_paper_acceptance_drill.py` against disposable PostgreSQL databases. It exercises a synthetic tenant's idempotent virtual seed, immutable order intent, partial fill, retry/conflict handling, bar-budget enforcement, fees, position updates, durable-state reconstruction across two separate `asyncio.run()` event loops in one process, full fill, compensating reversal, tenant isolation, balanced journals, and final isolation preflight. Each phase's database operations dispose their SQLAlchemy engines before the next event loop starts. This proves persistence across an event-loop boundary, not a killed process/container or worker crash. The staging workflow runs the same drill against the staging paper credential after an eligible successful paper-only staging deployment; it writes only synthetic tenant data under `customer_paper` and emits a redacted JSON evidence artifact. Synthetic acceptance evidence does not represent a real customer login or an authenticated HTTP/API acceptance test.

A staging preflight pass alone verifies identities and privileges; it does not prove end-to-end customer flows. Production sign-off still requires an independently verified backup and restore rehearsal, reconciliation of the production migration head and schema drift, and an authenticated AAL2 test-customer API drill. Keep all real-money trading, payouts, withdrawals, custody transfers, and customer-fund movement disabled. The live database must not be migrated or Alembic-stamped until a restorable backup and recovery rehearsal have been independently verified.

## Promotion/release decision

All four gates must be backed by reviewable source artifacts and passing tests on the exact commit. The signed docs, key IDs, raw source hashes, model fingerprints, trial archive IDs and PostgreSQL preflight/reconciliation outputs should be kept as release artifacts with immutable checksums. Test fixtures, CI synthetic values, prospective trial rows, or a green code suite are not substitutes. Keep customer paper execution disabled and all live trading, payouts, withdrawals and customer-fund movement disabled until separate release evidence and approvals are complete.
