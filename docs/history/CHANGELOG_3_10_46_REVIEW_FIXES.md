# 3.10.46 review fixes

- customer_funds.settle_realized_pnl / settle_trading_fee: new `strict` flag (default True = old behaviour).
  With `strict=False` (used by fill handling, where the broker fill already happened) the loss/fee is absorbed up to the
  customer's balance and the remainder is journaled to `ASSET:RECEIVABLE:CUSTOMER_DEFICIT` with a CRITICAL
  `CUSTOMER_DEFICIT:<ref>` incident. Balances never go negative (DB CHECK constraints `ck_customer_ledger_*_nonnegative`
  from migration 0032 forbid it), and journals stay balanced.
- execution.py: both fill-time call sites pass `strict=False`.
- trading_core.load_model: the artifact is read once; the hash/signature are verified on those bytes and
  `joblib.load` deserializes the same bytes from memory (closes the verify-then-reload swap window).
- tests/test_ledger_deficit_and_model_load_3_10_46.py added.

## Deliberately NOT changed
- Settlement idempotency keys still use `int(new_filled * 1_000_000)`. Changing to `round()` would produce different keys
  for already-posted fills and could double-post on retry after deploy. Move to broker fill/execution ids in a migration.
- `EXPENSE:TRADING_FEES` is credited for fees. Whether that should be a venue asset or platform revenue is an accounting
  policy decision for your accountant.
- LIVE fills never call `settle_trading_fee` (only the PAPER path does) and live trades don't capture the broker fee.
  Must be designed with broker fee parsing + idempotency before `customer_live_trading_enabled` is turned on.

## Round 2
- LIVE fee gap closed: app/live_fees.py parses ccxt `fee`/`fees` (quote-currency, base-currency, or unpriceable -> conservative taker
  estimate flagged as a HIGH incident `LIVE_FEE_ESTIMATED:<trade>`); execution._prepare_live_fee/_post_live_fee charge the customer
  ledger the DELTA of the cumulative order fee exactly once from both the live-submit path and _apply_broker_snapshot (reconcile).
  Fees are never lowered automatically; maker rebates are not credited.
- deploy/cloud-run-worker-deploy.sh: BACKUP_RECOVERY_URL is now required and passed. Startup refuses to run in production without it
  (require_backup_recovery_config defaults True) and the worker script never set it, so the worker pool could not boot.
- Tests: Postgres ledger concurrency against the REAL ledger code (the 3_10_39 test only exercises a probe table), live fee
  settlement, fake-broker reconcile drill, fee parser, research stats.
- scripts/exchange_sandbox_drill.py: operator-run venue-testnet drill (connectivity, fee-data location, market fill, duplicate
  clientOrderId, open/cancel/double-cancel, fee extraction vs venue trade list). `--dry-run` needs no network.
- app/research_stats.py: formal Probabilistic/Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014). NOT yet wired into the promotion gate.

## Findings not fixed here
- execution.py computes `reduce_only` for the live gate but calls broker.market_order(..., False): the exchange never receives reduceOnly.
- None of the new DB/concurrency/drill tests were executed in the authoring sandbox (no network, no sqlalchemy/Postgres/ccxt).
- DSR/PBO exist as a library only; the promotion gate still uses the internal multiple-testing proxy.

## Round 3
- execution.py: exchange-level `reduceOnly` is now sent on live closes (it was computed for the gate but hard-coded False on the order).
  Only for derivatives markets (`swap/future/...`, spot venues reject the parameter) and only when the order size does not exceed the
  opposing position (a flip must not be sent reduce-only). The gate's own `reduce_only` flag is unchanged.
- research_validation.deflated_sharpe_report + optional `min_dsr` in oos_promotion_gate (default None = behaviour unchanged; with a floor set,
  a missing DSR fails closed). research_engine attaches a formal DSR to the ensemble OOS and to every per_strategy_oos entry, counting
  components + parameter variants (+AI) as trials. config: `research_min_deflated_sharpe` (default 0.0 = OFF; 0.95 recommended once you
  have seen real values). main.py's promotion gate passes it.
- Caveat: the trial count only covers variants evaluated inside one research run. Repeated runs on the same data accumulate hidden
  trials; keep a persistent trial counter before trusting a DSR near the floor.
- Not executed here: research_engine end to end (sandbox lacks pydantic/lightgbm/sklearn); verified by compile + unit tests of the pieces.

## Round 4
- customer_funds.ledger_invariant_report + read-only `GET /api/admin/ledger/invariants`: proves every journal balances and each customer's
  AVAILABLE/TRADING_RESERVED/WITHDRAWAL_RESERVED equals credits-debits posted to its liability account; opens CRITICAL
  `LEDGER_INVARIANT_BREACH` on failure. Not yet scheduled -- call it from the worker or an external cron and alert on non-200/`ok:false`.
  CAUTION: unrun; a drift on first execution may mean a real code path mutating balances without a journal, or legacy data predating
  journals. Investigate before assuming the check is wrong.
- CI: `pyflakes app scripts tests` step (pyflakes already in requirements-dev per the earlier review).
- Correction to my earlier review: custody reconciliation DOES exist (`/api/admin/custody/reconciliation`, on-chain TRON vs customer liabilities,
  monitoring-only, on demand). What is missing is scheduling/alerting on it and an exchange-venue-balance vs reserved check.
