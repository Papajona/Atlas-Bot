# 3.10.45 post-review fixes

- db.py: Incident index referenced nonexistent `created_at`; now `opened_at` (matches migration 0033). App failed to import.
- customer_funds.settle_trading_fee: journal now debits the customer liability bucket(s) actually reduced (AVAILABLE and/or TRADING_RESERVED, split correctly) and credits EXPENSE:TRADING_FEES.
- customer_funds.settle_realized_pnl: idempotency check now runs before the affordability check so retries are no-ops.
- main.py: 8 `_safe_http_error` calls had swapped arguments (caused 500 TypeError); corrected.
- requirements-dev.txt: added pytest-asyncio.
- tests: replaced brittle source-text assertion for fee reserve consumption.
- Open: pip-audit flags ecdsa 0.19.2 (PYSEC-2026-1325, via bip-utils); PostgreSQL/Redis/sandbox gates still required.
- Verified: 368 passed, 1 skipped (PostgreSQL drill needs a live DB).

## Second pass (verified with pyflakes + runtime smoke)
- execution.py: added missing `or_`, `make_signal_id`, `client_order_id` imports and a best-effort `audit()` helper (new `audit_chain.record_audit`); `amount` -> `quantity` in live order command. Previously NameError on every order/reconcile/stop.
- main.py: initialised `_customer_jwks_cache` / `_jwks_refresh_lock` (all customer JWT checks failed); mounted `/static` and created `templates` (/, /admin returned 500); TemplateResponse uses the current Starlette signature.
- main.py: `customer_bot_status` was missing `for r in rows`; `customer_start_bot` now fetches derivatives context and fails closed on unavailable live context.
- main.py: route `release_withdrawal` shadowed the ledger function; ledger import aliased `ledger_release_withdrawal` (reject/fail/reconcile paths raised TypeError, stranding reserves).
- Withdrawals: reject only from PENDING/PARTIALLY_APPROVED/APPROVED; reconcile row-locked and only from SUBMITTING/SUBMITTED/UNKNOWN.
- New tests/test_static_name_integrity.py (undefined names, shadowed redefinitions, pages render). requirements-dev: pyflakes.
- NOT changed: FinancialNumeric returns Decimal while trading code multiplies by floats (TypeError confirmed) - needs a decision.

## Third pass
- db.FinancialNumeric now materializes float (was Decimal), matching its docstring and the float-native trading code. Fixes `Decimal * float` TypeError in paper fills, deposit scanner, funding webhook, withdrawal creation. Custodial ledger unaffected (own Numeric(38,6) columns).
- execution.py: entry-cooldown comparison tolerates naive datetimes (SQLite).
- New tests/test_paper_order_e2e.py: real paper buy+sell through execute_signal.
- Observation (not changed): a sell that reduces an existing long still required a protective stop in the discipline gate; verify intended behaviour.

## Fourth pass — the nine remaining items from the pasted review
- customer_funds.py: all ledger amounts now quantized to the Numeric(38,6) column precision (half-even);
  reserve_trading/reserve_withdrawal round UP so a reserve is never short by a rounding remainder.
- post_deposit: idempotency key is now `deposit:{provider}:{reference}`, so two providers cannot
  reuse each other's reference and suppress a real credit. The pre-existing un-namespaced
  `deposit:{reference}` key is still honoured for deposits posted before this change.
- settle_withdrawal: when the payout exceeds the customer's withdrawal_reserved balance, this is now
  logged at CRITICAL and opens (or refreshes) an Incident in the same transaction, instead of clamping
  silently.
- main.py `_usdt_tron_monitor_loop`: a deposit skipped for a transient reason (receipt not yet indexed,
  too few confirmations) now defers the cursor via `_next_tron_cursor()` instead of letting
  `highest_ts` (still updated unconditionally) advance past it — the deposit is re-scanned next poll.
  A deposit stuck for >24h logs a warning and stops pinning the cursor. Identical transfers inside one
  TRON transaction (same tx/sender/recipient/value/timestamp) now get distinct provider references via
  `_tron_deposit_reference()` (`...#1`, `...#2`, ...); the first occurrence keeps the historical format.
- CustomerWithdrawalCreate / WithdrawalCreate: `amount` now rejects more than 6 decimal places
  (matches the ledger's Numeric(38,6) column) via a field_validator.
- `_withdrawal_recovery_loop`: WITHDRAWAL_RECOVERY_DUE is now throttled per withdrawal id to at most
  once per 15 minutes via `_should_alert_recovery()`, instead of firing on every poll while stuck.
- funding_webhook: the PENDING->CONFIRMED transition now credits the amount stored on the original
  FundingTransaction row, not the payload's amount (a re-sent or malicious webhook could otherwise
  credit an arbitrary figure). A mismatch is audited as FUNDING_AMOUNT_MISMATCH rather than silently
  trusted. Deposits from this path are also provider-namespaced per the point above.
- execution.py `_apply_fill_to_position` (flip branch): the reserve for the new side of a flipped
  position is now attempted inside a savepoint. If the customer's cash can't cover it, the position and
  filled quantity are still recorded (the broker fill already happened and must not be lost) and a
  CRITICAL `POSITION_FLIP_RESERVE_DEFICIT` incident is opened instead of raising out of fill handling.
- New tests/test_money_safety_fixes.py covers all nine items with behavioural (not source-text) tests.
- Updated tests/test_tron_deposit_reconciliation_3_10_18.py, which pinned the old unconditional
  cursor-advance line.
- Verified: 383 passed, 1 skipped (PostgreSQL drill needs a live DB), from a clean unzip.

## Fifth pass — intelligence-layer audit (adaptive_bot, meta_labeling, strategy_ai,
## strategy_router, research_engine, market_intelligence, daily_research) + arbitrage
- pyflakes on these 7 files was clean of undefined names/shadowed imports (unlike the
  earlier main.py/execution.py findings) -- only 4 harmless unused imports/vars, removed.
- meta_labeling.py: the module's own internal walk-forward (used for the live per-bar
  meta-label probability gate) did not purge/embargo its fold boundaries, unlike
  research_validation.purged_walk_forward_splits() used everywhere else in this codebase.
  Since each label depends on price up to `horizon` bars ahead, training rows right at a
  fold boundary leaked information into the following test fold. Added the same purge/
  embargo discipline (by `horizon`) so the reported oos_take_rate is honest.
- Daily Yahoo Finance ingestion already existed end-to-end (gather_market_intelligence ->
  yahoo_history, scheduled via _daily_research_loop at settings.daily_research_hour_utc,
  default symbols SPY/QQQ/BTC-USD/ETH-USD/EURUSD=X/GC=F/CL=F) -- confirmed, not newly added.
  What it lacked: the computed macro/news risk snapshot (Fed/ECB feeds + broad trend) was
  never persisted past the single async call that produced it, so nothing later could use
  "knowledge of the market" from a prior day. Added `research_runs.macro_risk_state` /
  `macro_risk_score` (migration 0042_research_run_macro_context.py), `_persist_research_run`
  now stores them, and a new `daily_research.latest_macro_context()` reads the latest
  snapshot. `_run_persisted_adaptive_bot_cycle`'s live decision path now includes this as
  `macro_context` in the packet sent to `dual_ai_trade_safety_review` -- advisory context for
  the LLM veto only, it does not gate on its own and adds no new execution authority.
  NOTE: this is global macro context, not matched to the specific trading symbol -- ResearchRun
  uses Yahoo Finance ticker formats (BTC-USD, EURUSD=X) that don't line up with live
  exchange/broker symbol formats (BTC/USDT, EUR_USD, Deriv symbols). A real per-symbol mapping
  is future work; flagged rather than faked.
- OANDA's demo-only restriction: confirmed as intentional (backtesting/training/strategy
  research use, not live execution) per your note -- left unchanged, no longer flagged as a gap.
- Arbitrage: `binance_arbitrage_live.py`/`binance_arb_reconcile.py` (the live triangular
  executor) are deliberately walled off (raises 409/RuntimeError) until integrated with the
  unified ledger/outbox -- correct, left untouched. The active path is `arbitrage_paper`,
  which used a static customer-supplied `start_quote` per request with no link between
  cycles. Added `arbitrage.compounded_stake()`: an advisory next-stake recommendation built
  from the customer's own trailing simulated PAPER_CANDIDATE history -- grows only off
  *cumulative* simulated edge (never a single lucky cycle), shrinks toward (never below)
  a floor after a losing simulated record, and is hard-capped at the same
  arbitrage_max_capital_usdt/account-equity limit that already gates every request.
  `arbitrage_paper`'s response now includes `recommended_next_stake`; the customer's actual
  next `start_quote` is still their own choice and still checked against the same cap.
- New tests/test_research_and_arbitrage_hardening.py (11 tests) covers all of the above.
  tests/test_static_name_integrity.py extended with an explicit pyflakes gate over these
  7 files by name (not just "app" broadly) so this class of bug can't recur silently here.
- Verified: 395 passed, 1 skipped (PostgreSQL drill), from a clean unzip.
- NOT independently verified: migration 0042 applying end-to-end against a live
  PostgreSQL instance (none available in this environment) -- it follows the exact same
  safe idiom as 0041 (_has_col guard + plain add_column, no constraint changes), but
  alembic against SQLite in this repo has a pre-existing, unrelated limitation (batch-mode
  ALTER support) that predates this change and blocks testing any migration chain end-to-end
  outside Postgres here.

## Sixth pass — implemented the top recommendation from the competitive review
- Wired `correlation_risk.adjusted_group_exposure()` into the live per-symbol decision
  pipeline (`_run_persisted_adaptive_bot_cycle` in main.py) -- confirmed dead code
  (imported, never called) in the prior review. Inserted right after the per-trade
  quantity is sized and before the runtime gate: queries the customer's own open
  positions, checks the proposed trade against `settings.correlation_group_cap_usd`
  (already existed in config, unused), and on a breach SHRINKS quantity to the
  remaining headroom (same treatment as the existing derivatives_context ELEVATED
  multiplier) rather than hard-blocking, with a new `correlation_group_cap` NO_TRADE
  stage and `ADAPTIVE_BOT_CORRELATION_CAP` audit entry if headroom hits zero.
- Found and fixed a real, previously-hidden bug while writing the regression test for
  the above: `correlation_risk.risk_group()` only matched the pre-underscore segment of
  a normalized symbol against `_GROUPS`. That works for crypto (dict keys are bare
  base-asset tickers like "BTC") but `_GROUPS` keys FX/metals/energy by the FULL pair
  ("EUR_USD", "XAU_USD") -- so `risk_group("EUR_USD")` looked up "EUR", which is never a
  key, and silently fell through to "OTHER". This wasn't just "ungrouped": EVERY
  forex/metals/energy symbol fell into that SAME "OTHER" bucket together, so e.g. a gold
  position would have incorrectly counted against an unrelated JPY proposal's exposure
  cap, while two genuinely correlated FX pairs (EUR_USD, GBP_USD) only grouped together
  by the same coincidence. Fixed by trying the full normalized symbol against `_GROUPS`
  first, falling back to the base-asset segment for the crypto case. This means the
  correlation cap I just wired in would have been silently wrong for every forex/
  commodity bot from day one had this not been caught before shipping.
- New tests/test_correlation_risk_gate.py (9 tests): correlation_risk.py had ZERO prior
  test coverage despite being imported in main.py -- covers risk_group() for both key
  styles (including the bug above as an explicit regression test), adjusted_group_exposure()
  aggregation/cap behavior for both crypto and FX, and two source-level checks confirming
  the gate is wired at the correct point in the pipeline and scoped to the correct
  customer_id (never cross-customer).
- Deliberately NOT implemented this pass: the 24h-vs-1-2-week retrain cadence tradeoff
  flagged repeatedly in prior reviews. That's a product decision (how much walk-forward
  evidence you're willing to trade for faster adaptation), not a bug -- still open,
  still needs your explicit call before I touch adaptive_retrain_hours/research_min_train.
- ecdsa CVE and the unverified-against-Postgres migration (0042): unchanged, still open.
- Verified: 404 passed, 1 skipped (PostgreSQL drill), from a clean unzip, full-app
  pyflakes clean.

## Seventh pass — resolved the two biggest open items from prior reviews for real
Instead of repeating the same two flags a third time, I actually resolved them.

### 1. ecdsa CVE: investigated and documented as an accepted, low-risk, reviewed exception
Researched PYSEC-2026-1325 directly: upstream has stated side-channel fixes are out of
scope and there is no planned fix for any version -- this was never fixable by waiting
or upgrading. Traced every bip-utils call site in this codebase: all of it is public-key-
only TRON address derivation/validation (`derive_usdt_tron_address_from_xpub`,
`validate_tron_account_xpub`, both explicitly public-only per their own code), and actual
custody signing already happens outside this app entirely (`ExternalSignerPayoutProvider`).
The advisory's vulnerable operations (signing, key generation, ECDH) are not reachable
through this app's actual usage. Documented as a reviewed, accepted exception in
SECURITY.md with the specific evidence, not a blanket "ignore."

### 2. Migration chain verified against a REAL PostgreSQL instance -- found and fixed SIX
previously-undiscovered bugs that made `alembic upgrade head` impossible on any fresh
Postgres database. None of these were caught before because this app's SQLite test suite
creates tables via `Base.metadata.create_all()` directly and never actually runs Alembic
-- this migration chain had, as far as I can find, never been exercised end-to-end before.
Installed PostgreSQL 16 in this sandbox and ran the full 0001->0042 chain against a clean
database to find these:

- **0015**: `exchange_connectors.updated_at` was `NOT NULL` with no default, and the
  migration's own seed `bulk_insert` didn't supply one -- failed immediately on a fresh
  DB. Added `server_default=sa.func.now()`, matching the pattern the same migration
  already used for its `enabled` column.
- **0030**: `op.add_index(...)` -- not a real Alembic API (the function is
  `op.create_index`). This means the financial-precision migration (Float->Numeric(38,18)
  across 17 tables) could never have completed on Postgres. Fixed the call name.
- **0038**: `op.execute(sa.text(...), {"legacy": ...})` -- `op.execute` (unlike a raw
  SQLAlchemy `Connection.execute`) does not accept a parameters dict as a second
  positional argument; only ran on the `dialect.name == "postgresql"` branch, so
  completely invisible to SQLite tests. Fixed using `.bindparams()` on the `sa.text(...)`
  object, the same pattern migration 0030 already used correctly elsewhere.
- **0039**: `positions.entry_trade_id` is a real column on the ORM model
  (`app/db.py`) but no migration ever added it to the actual table -- the composite FK
  this migration builds on it would fail with "column does not exist" on a fresh DB.
  Added the missing `op.add_column` + index, idempotently, immediately before the FK.
- **0040 and 0041** (two separate instances of the same bug): both bundled several
  top-level SQL statements (CREATE FUNCTION / DROP TRIGGER / CREATE TRIGGER) into one
  `op.execute(sa.text("""..."""))` call. asyncpg -- the actual async driver this app's
  engine uses -- refuses with "cannot insert multiple commands into a prepared
  statement." This means the ledger-balance-validation and immutability triggers, two of
  the most safety-critical pieces in the whole schema, could never have been installed
  through this app's own engine. Split each into one `op.execute()` per statement, every
  line of SQL unchanged, only the boundaries moved.
- Ran an AST-based sweep (not a brittle regex) across every migration file for both the
  `op.add_index` and `op.execute(stmt, dict)` bug classes after each fix -- confirmed no
  further instances anywhere in the chain.

**After all six fixes**: `alembic upgrade head` completes with exit code 0 against a
clean PostgreSQL 16 database, landing at `0042_research_run_macro_context` as expected.
Went further than "it didn't error" -- functionally proved the ledger triggers actually
work: inserted an unbalanced journal and confirmed `atlas_validate_ledger_journal`
correctly rejected it; inserted a properly balanced, committed journal and confirmed the
separate immutability trigger then correctly blocked an UPDATE on it
("Ledger journal records are immutable"). The full SQLite suite was rerun after every
fix in this pass: still 404 passed, 1 skipped throughout, zero regressions.

**Deliberately not done this pass**: the 24h-vs-longer retrain cadence is still an open
product decision, not touched. Given the scale of what the Postgres verification turned
up, I prioritized finishing and proving that over a feature change that still needs your
call.

## Eighth pass — the retrain-cadence recommendation, implemented
Deep-thought recommendation: don't change adaptive_retrain_hours itself (a genuine
product trade-off between adaptation speed and evidence volume -- still your call), but
close the actual bug-shaped gap underneath it: a challenger could be promoted to live-
eligible CHAMPION off a SINGLE day's passing walk-forward gate.

- `AdaptiveModelPolicy` gained `consecutive_passes_required: int = 2`. A challenger that
  passes the existing gate (absolute thresholds + incumbent-tolerance check, unchanged)
  is now promoted only once it has passed on this many consecutive daily evaluation
  cycles. A streak is tracked in a small sidecar file next to the model
  (`{model_path}.candidate_streak.json`) and is reset to zero by ANY failing cycle --
  consecutive means consecutive, an earlier partial streak never carries over. The field
  is the lever toward "1-2 weeks of evidence" (raise it, e.g. to 7-10) without touching
  retrain frequency; 2 is the conservative floor that closes the one-lucky-day gap.
- New `ensure_adaptive_model()` status: `CHALLENGER_PASSED_AWAITING_CONFIRMATION` for a
  pass that hasn't yet reached the required streak length. The existing champion model
  file is left completely untouched in this state -- bots keep trading on the current
  (already-promoted) champion exactly as if the challenger had simply not run yet.
  Added to the `ModelExperiment` logging allowlist in main.py for observability.
- A successful promotion clears the streak file, so the next challenger cycle starts
  clean.
- New tests/test_adaptive_model_promotion_streak.py (7 tests): single-pass-does-not-
  promote, two-consecutive-passes-promotes (default policy), a failure resets the streak
  (and a single pass after the reset is NOT enough on its own), the required count is
  configurable, promotion clears the streak file, and a regression guard that the default
  can never silently drop back to 1 (which would reinstate the exact behavior this
  feature exists to close).
- Updated tests/test_strategy_engine.py::test_adaptive_champion_promotion_keeps_model_
  integrity, which asserted promotion on a single call -- now calls twice (matching the
  new default policy) before asserting the original integrity checks, which are
  otherwise unchanged.

## Ninth pass -- fact-check sweep: one new, real, fixable CVE found and resolved
Reran the full verification stack (pyflakes, full suite, bandit, pip-audit, and a real
PostgreSQL 16 migration run) after the above. Everything held -- except pip-audit surfaced
a vulnerability that wasn't present in earlier passes:

- `urllib3==2.7.0` (pulled in by `ccxt`'s exact pin) carries three CVEs disclosed since
  the last check: CVE-2026-97687 (HTTPS proxy TLS settings can be ignored/overridden),
  CVE-2026-97688 (chunked Deflate streaming can enter an infinite loop), CVE-2026-97689
  (unbounded chunk-size-line buffering). All three are relevant to this app's exchange/
  broker HTTP traffic. Unlike ecdsa, this one has a real fix: `ccxt==4.5.85` (one patch
  above the prior pin) already requires `urllib3==2.8.0`, so bumped `requirements.txt` to
  `ccxt==4.5.85` rather than fighting ccxt's exact-pin with a standalone urllib3 pin.
  Verified: pip-audit now reports only the already-reviewed, accepted ecdsa finding.
- Verified (again): 410 passed, 1 skipped, pyflakes clean, bandit 0 medium/high, and
  `alembic upgrade head` against a fresh real PostgreSQL 16 database completes with exit
  code 0, landing at 0042 as expected -- confirms nothing in this pass regressed the
  migration chain fixed two passes ago.
