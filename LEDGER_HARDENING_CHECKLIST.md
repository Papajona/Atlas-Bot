# Ledger Concurrency Hardening: Production Sign-Off Checklist

## Fix Summary

**Root Cause:** Time-of-check-to-time-of-use (TOCTOU) race condition in ledger balance mutations. Multiple concurrent transactions could read stale balance, both pass validation, and overdraw customer account or double-book reserves.

**Fix Pattern:** 
- Explicit row-level locking with `SELECT ... FOR UPDATE` on `CustomerLedgerAccount`
- Fresh balance read under lock before validation
- All mutations (balance + journal) in single transaction
- Idempotency enforcement via unique constraints on `LedgerJournal.idempotency_key` and `LedgerEntry.idempotency_key`
- Transaction retry on `IntegrityError` instead of allowing duplicate writes

**Affected Functions:**
- `post_journal()` — idempotency lock added
- `reserve_trading()` — fresh read under lock, TOCTOU guard added
- `reserve_withdrawal()` — fresh read under lock, TOCTOU guard added
- `settle_realized_pnl()` — fresh read under lock, TOCTOU guard added
- `settle_trading_fee()` — fresh read under lock, TOCTOU guard added
- `get_or_create_ledger()` — existing savepoint + IntegrityError retry confirmed

---

## Verification Requirements

### ✓ Phase 1: Code Review (DONE)

- [x] All ledger mutation paths use `SELECT ... FOR UPDATE`
- [x] All balance checks happen after lock acquisition
- [x] Journal/entry insertion is in same transaction as balance mutation
- [x] Idempotency keys are enforced as unique constraints in schema
- [x] No raw balance read → check → write without lock pattern remains

### ✓ Phase 2: Unit & Integration Tests (IN PROGRESS)

Tests must pass in **PostgreSQL environment** (not SQLite):

**Required tests:**
- [x] `tests/test_postgres_ledger_concurrency_3_10_46.py::test_concurrent_withdrawal_reserves_never_overdraw` — 25 concurrent reserves against 100 available, only 3 should succeed
- [x] `tests/test_postgres_ledger_concurrency_3_10_46.py::test_duplicate_reference_under_contention_posts_once` — 20 concurrent reserves with same reference, posts exactly once
- [x] `tests/test_postgres_ledger_concurrency_3_10_46.py::test_concurrent_fee_and_loss_settlement_never_goes_negative_and_books_deficit` — concurrent fees and losses remain balanced, deficit is booked
- [x] `tests/test_ledger_invariants_3_10_46.py::test_invariants_hold_after_normal_and_deficit_activity_then_detect_tampering` — invariant checker catches drift

**New comprehensive coverage (all mutation paths):**
- [x] `tests/test_ledger_all_paths_concurrent_3_10_46.py::test_concurrent_trading_reserves_never_overdraw` — reserve_trading under contention
- [x] `tests/test_ledger_all_paths_concurrent_3_10_46.py::test_concurrent_release_trading_idempotent` — release_trading idempotency
- [x] `tests/test_ledger_all_paths_concurrent_3_10_46.py::test_concurrent_withdrawal_reserve_and_release_interleaved` — mixed reserve/release
- [x] `tests/test_ledger_all_paths_concurrent_3_10_46.py::test_concurrent_settle_withdrawal_never_exceeds_reserve` — settle_withdrawal bounds checking
- [x] `tests/test_ledger_all_paths_concurrent_3_10_46.py::test_concurrent_trading_and_withdrawal_reserves_independent` — isolation of reserve buckets
- [x] `tests/test_ledger_all_paths_concurrent_3_10_46.py::test_concurrent_fee_settlement_caps_to_available_plus_reserved` — fee cap under contention
- [x] `tests/test_ledger_all_paths_concurrent_3_10_46.py::test_concurrent_pnl_and_fee_settlement_combined` — combined settlement stress
- [x] `tests/test_ledger_all_paths_concurrent_3_10_46.py::test_concurrent_get_or_create_ledger_no_duplicate` — ledger creation race
- [x] `tests/test_ledger_all_paths_concurrent_3_10_46.py::test_duplicate_idempotency_across_all_functions` — idempotency end-to-end

### ✓ Phase 3: CI/CD Enforcement (ADDED)

**New workflow:**
- `.github/workflows/ledger-concurrency-check.yml` — **MANDATORY** for any changes to:
  - `app/customer_funds.py`
  - `app/db.py`
  - ledger test files

**Behavior:**
- Sets up PostgreSQL 15 service
- Runs all schema migrations
- Runs `test_ledger_invariants_3_10_46.py` (SQLite baseline)
- Runs `test_postgres_ledger_concurrency_3_10_46.py` (BLOCKER if fails)
- Runs `test_ledger_all_paths_concurrent_3_10_46.py` (new, BLOCKER if fails)
- Workflow fails if Postgres tests do not execute (prevents CI bypass)

### ○ Phase 4: Staging Validation (BEFORE PRODUCTION)

Before deploying to production:

1. Run the full ledger concurrency suite in a staging environment:
   ```bash
   export ATLAS_POSTGRES_URL='postgresql+asyncpg://staging-user:pass@staging-db/staging_db'
   pytest -q tests/test_postgres_ledger_concurrency_3_10_46.py tests/test_ledger_all_paths_concurrent_3_10_46.py tests/test_ledger_invariants_3_10_46.py -v
   ```

2. Verify invariant report with staging data:
   ```python
   from app.customer_funds import ledger_invariant_report
   from app.db import SessionLocal
   
   async with SessionLocal() as db:
       report = await ledger_invariant_report(db)
       assert report["ok"] is True, f"Ledger invariant breach: {report}"
   ```

3. Monitor production logs for:
   - `ledger_invariant_breach` incidents (should not occur)
   - `CUSTOMER_DEFICIT` incidents (expected, not a blocker if booked correctly)
   - Lock timeout errors (indicates contention, may need pool tuning)

### ✓ Phase 5: Documentation

- [x] LEDGER_HARDENING_CHECKLIST.md (this file) — explains fix and verification
- [x] Code comments in `app/customer_funds.py` — why locks are needed
- [x] CI workflow documentation — how mandatory tests work
- [x] Test docstrings — what each concurrency test proves

---

## Sign-Off Criteria

### ✓ Ready for Merge IF:

- [x] All code review items pass
- [x] All unit and integration tests pass on PostgreSQL
- [x] CI workflow is enabled and documented
- [x] No regressions in existing functionality
- [x] Idempotency is working as designed (verified in tests)

### ✗ NOT Ready for Production IF:

- [ ] Postgres concurrency tests have not been run
- [ ] CI workflow is not enabled (can be bypassed)
- [ ] Staging invariant report shows drift or breaches
- [ ] Any ledger mutation function is missing `SELECT ... FOR UPDATE`
- [ ] There are uncommitted changes to ledger paths

---

## Deployment Instructions

1. **Merge this branch** to main (requires CI to pass)
2. **Run staging validation** (Phase 4 above)
3. **Deploy to production** with Postgres backed
4. **Monitor** for invariant breaches and CUSTOMER_DEFICIT incidents
5. **Alert on** lock timeout or serialization failures

---

## Known Limitations

- SQLite in-memory tests are not sufficient proof; only PostgreSQL tests are definitive
- The fix assumes SERIALIZABLE isolation or deferred unique constraints; verify DB settings
- Lock timeout is set to 5000ms in CI; production may need tuning based on load
- If transaction throughput drops post-deployment, consider connection pool size

---

## Rollback Plan

If a ledger integrity issue is discovered in production:

1. **Immediate:** Set kill_switch to disable trading and withdrawals
2. **Investigate:** Run `ledger_invariant_report()` to identify affected customers
3. **Rollback:** Revert to previous version and pause new ledger mutations
4. **Reconcile:** Manually reconcile customer deficits using incident log data
5. **Re-test:** Repeat Phase 4 validation before re-enabling

---

## Approvals

| Role | Name | Date | Status |
|------|------|------|--------|
| Code Review | (awaiting) | | ⏳ |
| QA / Testing | (awaiting) | | ⏳ |
| DevOps / Infra | (awaiting) | | ⏳ |
| Security | (awaiting) | | ⏳ |
| Release Manager | (awaiting) | | ⏳ |

---

## References

- Commit: `6e8583594bb3d0fa970a00932b672850d6ae8c3a` (hardening/select-for-update-patches)
- Schema migrations: `alembic/versions/` (customer_ledger_accounts table)
- Related docs: `CUSTOMER_FUNDS_ARCHITECTURE.md`
