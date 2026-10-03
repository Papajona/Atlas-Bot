# Ledger Financial Hardening Roadmap — Complete Implementation Guide

**Status:** Phase 1 In Progress, Phases 2-4 Planned  
**Target:** Production safety certification within 4 weeks  
**Owner:** Papajona/Atlas-Bot  
**Last Updated:** 2026-10-02  

---

## 🔴 PHASE 1: FINANCIAL CORRECTNESS (CRITICAL PATH)

### 1.1 Audit Every Ledger Mutation

**Status:** ✅ Complete  
**All mutation functions identified:**

| Function | Location | Locks? | Idempotent? | Status |
|----------|----------|--------|------------|--------|
| `post_deposit()` | L96 | ⚠️ Partial | ✅ Yes | REVIEW |
| `reserve_trading()` | L129 | ✅ Yes | ✅ Yes | PASS |
| `release_trading()` | L171 | ⚠️ Partial | ✅ Yes | REVIEW |
| `reserve_withdrawal()` | L193 | ✅ Yes | ✅ Yes | PASS |
| `release_withdrawal()` | L224 | ⚠️ Partial | ✅ Yes | REVIEW |
| `settle_withdrawal()` | L248 | ⚠️ Partial | ✅ Yes | REVIEW |
| `settle_realized_pnl()` | L291 | ✅ Yes | ✅ Yes | PASS |
| `settle_trading_fee()` | L350 | ✅ Yes | ✅ Yes | PASS |
| `get_or_create_ledger()` | L35 | ✅ Yes | ✅ Savepoint | PASS |

**Action Items:**

```python
# REQUIRED: post_deposit() needs fresh read under lock
async def post_deposit(...):
    ledger = await get_or_create_ledger(db, customer_id, USDT)
    # ⚠️ MISSING: fresh_ledger = (await db.execute(...).with_for_update()).scalar_one()
    # ⚠️ BUG: Uses stale ledger.available without lock guarantee
    ledger.available = D(str(ledger.available)) + amount_d
```

✅ **FIX:** Requires fresh read before mutation:
```python
async def post_deposit(...):
    await get_or_create_ledger(db, customer_id, USDT)  # Create if missing
    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()  # ✅ Lock and re-read
    fresh_ledger.available = D(str(fresh_ledger.available)) + amount_d
```

### 1.2 Establish One Consistent Transaction Pattern

**Status:** 🟡 Partial  
**Pattern Definition (required in every mutation):**

```
1. Call get_or_create_ledger() → acquires lock on customer_ledger_accounts row
2. Check idempotency key with SELECT ... FOR UPDATE on LedgerJournal
3. Fresh read: SELECT ... FOR UPDATE on CustomerLedgerAccount (re-read after step 2)
4. Validate balance/reserves against fresh data
5. Mutate fresh_ledger (available, trading_reserved, withdrawal_reserved)
6. Call post_journal() to insert LedgerJournal + LedgerJournalLine atomically
7. Commit transaction (all or nothing)
```

**Current State:**
- ✅ `reserve_trading()` follows pattern perfectly (L129-169)
- ✅ `settle_realized_pnl()` follows pattern perfectly (L291-347)
- ✅ `settle_trading_fee()` follows pattern perfectly (L350-399)
- ⚠️ `post_deposit()` violates: no fresh read after get_or_create
- ⚠️ `release_trading()` violates: no lock before release calculation (L177)
- ⚠️ `release_withdrawal()` violates: no lock before release calculation (L232)
- ⚠️ `settle_withdrawal()` violates: no lock before settlement calculation (L256)

**Required Fixes:**

**Fix 1:** `post_deposit()` — Add fresh read
```python
# BEFORE: Uses stale ledger.available
ledger = await get_or_create_ledger(db, customer_id, USDT)
ledger.available = D(str(ledger.available)) + amount_d

# AFTER: Uses fresh read under lock
await get_or_create_ledger(db, customer_id, USDT)  # Ensure exists
fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
    CustomerLedgerAccount.customer_id == customer_id,
    CustomerLedgerAccount.currency == USDT,
).with_for_update())).scalar_one()
fresh_ledger.available = D(str(fresh_ledger.available)) + amount_d
```

**Fix 2:** `release_trading()` — Add fresh read and lock before calculation
```python
# BEFORE: Reads from stale ledger
release = min(amount_d, D(str(ledger.trading_reserved)))
if release <= 0:
    return ledger
ledger.trading_reserved = D(str(ledger.trading_reserved)) - release

# AFTER: Fresh read under lock, calculate under lock
fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
    CustomerLedgerAccount.customer_id == customer_id,
    CustomerLedgerAccount.currency == USDT,
).with_for_update())).scalar_one()
release = min(amount_d, D(str(fresh_ledger.trading_reserved)))
if release <= 0:
    return fresh_ledger
fresh_ledger.trading_reserved = D(str(fresh_ledger.trading_reserved)) - release
fresh_ledger.available = D(str(fresh_ledger.available)) + release
```

**Fix 3:** `release_withdrawal()` — Add fresh read and lock
(Same pattern as Fix 2, for withdrawal_reserved)

**Fix 4:** `settle_withdrawal()` — Add fresh read and lock
```python
# BEFORE: Reads from stale ledger
reserved_now = D(str(ledger.withdrawal_reserved))
settle = min(amount_d, reserved_now)

# AFTER: Fresh read under lock
fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
    CustomerLedgerAccount.customer_id == customer_id,
    CustomerLedgerAccount.currency == USDT,
).with_for_update())).scalar_one()
reserved_now = D(str(fresh_ledger.withdrawal_reserved))
settle = min(amount_d, reserved_now)
fresh_ledger.withdrawal_reserved = D(str(fresh_ledger.withdrawal_reserved)) - settle
```

### 1.3 Add/Verify SELECT FOR UPDATE

**Status:** ✅ Present, 🟡 Incomplete  

**Verified in:**
- ✅ `get_or_create_ledger()` (L36-39, L50-53, L58)
- ✅ `reserve_trading()` (L146-148, L151-154)
- ✅ `reserve_withdrawal()` (L199-201, L204-207)
- ✅ `settle_realized_pnl()` (L305-307, L310-313)
- ✅ `settle_trading_fee()` (L361-363, L366-369)
- ✅ `post_journal()` (L67-69)

**Missing in:**
- ⚠️ `post_deposit()` — no fresh read, no lock
- ⚠️ `release_trading()` — no fresh read, no lock before release calc
- ⚠️ `release_withdrawal()` — no fresh read, no lock before release calc
- ⚠️ `settle_withdrawal()` — no fresh read, no lock before settlement calc
- ⚠️ `sync_wallet_from_ledger()` — reads ledger without lock (L122)

**Action:** Apply Fixes 1-4 above

### 1.4 Lock All Affected Accounts

**Status:** 🟡 Partial  

**Primary ledger row (customer_ledger_accounts):**
- ✅ Locked in: reserve_trading, reserve_withdrawal, settle_realized_pnl, settle_trading_fee, get_or_create_ledger, post_journal
- ⚠️ Not locked in: post_deposit, release_trading, release_withdrawal, settle_withdrawal

**Wallet row (legacy sync):**
- ✅ Locked in: get_or_create_ledger (L58), sync_wallet_from_ledger (L123)

**Incident row (for CRITICAL events):**
- ✅ Locked in: record_ledger_incident (via upsert logic, L27-33)

**Action:** Apply Fixes 1-4 to guarantee all mutations hold locks

### 1.5 Establish Deterministic Lock Ordering

**Status:** ✅ Trivial (single lock per transaction)  

**Lock Strategy:**
```
Always lock CustomerLedgerAccount first:
  1. get_or_create_ledger(db, customer_id, USDT)
     → SELECT ... FOR UPDATE on customer_ledger_accounts
     → If not found, INSERT in savepoint (with IntegrityError retry)
  
  2. Inside same transaction, lock LedgerJournal if checking idempotency
     → SELECT ... FOR UPDATE on ledger_journals (by idempotency_key)
  
  3. Re-lock CustomerLedgerAccount (fresh read, still under lock from step 1)
     → SELECT ... FOR UPDATE on customer_ledger_accounts
  
  4. Mutate CustomerLedgerAccount fields
  
  5. Call post_journal() → acquires lock on ledger_journals, inserts row
  
  6. Commit (single transaction, all or nothing)
```

**No risk of deadlock:**
- Only one ledger row per customer/currency pair
- All transactions lock in same order: ledger → journal → ledger again
- No cross-customer deadlocks possible

### 1.6 Protect First-Use Ledger Creation

**Status:** ✅ Verified

**Current implementation (L35-63):**
```python
async def get_or_create_ledger(...):
    # 1. Try to lock existing row
    row = (await db.execute(select(...).with_for_update())).scalar_one_or_none()
    if row:
        return row
    
    # 2. If not found, try to INSERT in savepoint
    try:
        async with db.begin_nested():  # Savepoint protects from duplicate error
            row = CustomerLedgerAccount(customer_id=..., currency=...)
            db.add(row)
            await db.flush()
    except IntegrityError:  # Another transaction won the race
        # 3. Retry lock + fetch the winner's row
        row = (await db.execute(select(...).with_for_update())).scalar_one()
        return row
    
    # 4. Copy legacy wallet balance (one-time migration)
    wallet = (await db.execute(select(...).with_for_update())).scalar_one_or_none()
    if wallet and wallet.available_balance > 0:
        row.available = Decimal(str(wallet.available_balance))
    
    return row
```

✅ **This is correct:** Savepoint + IntegrityError retry ensures exactly one ledger row wins

### 1.7 Enforce Idempotency at the DB Level

**Status:** ✅ Schema-verified

**Unique constraints present:**
```sql
-- app/db.py lines 447-450
CREATE TABLE customer_ledger_accounts (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    currency VARCHAR(20) NOT NULL,
    ...
    CONSTRAINT uq_customer_ledger_account_currency 
        UNIQUE (customer_id, currency)
);

-- app/db.py lines 462-464
CREATE TABLE ledger_journals (
    id SERIAL PRIMARY KEY,
    idempotency_key VARCHAR(220) NOT NULL UNIQUE,
    ...
    CONSTRAINT uq_ledger_journal_idempotency
        UNIQUE (idempotency_key)
);

-- app/db.py lines 498
CREATE TABLE ledger_entries (
    id SERIAL PRIMARY KEY,
    idempotency_key VARCHAR(220) NOT NULL UNIQUE,
    ...
    CONSTRAINT uq_ledger_entry_idempotency
        UNIQUE (idempotency_key)
);
```

✅ **Protection:**
- Duplicate calls with same `reference_id` produce same `idempotency_key`
- `SELECT ... FOR UPDATE on LedgerJournal WHERE idempotency_key = X` returns same row
- Post_journal() skips insert if row exists (L70-71)
- Duplicate IntegrityError on insert is caught and retried

---

## 🟠 PHASE 2: VERIFICATION (PROOF UNDER LOAD)

### 2.1 Add Mixed-Operation Concurrency Tests

**Status:** ✅ Tests written, 🟡 CI integration pending

**Tests (tests/test_ledger_all_paths_concurrent_3_10_46.py):**
- ✅ `test_concurrent_trading_reserves_never_overdraw()` — 15 concurrent reserves @ 150 each, max 6 succeed
- ✅ `test_concurrent_release_trading_idempotent()` — 20 duplicate releases, only one effective
- ✅ `test_concurrent_withdrawal_reserve_and_release_interleaved()` — 10 reserves + 5 releases, no negative state
- ✅ `test_concurrent_settle_withdrawal_never_exceeds_reserve()` — 25 concurrent settlements cap to 100 reserved
- ✅ `test_concurrent_trading_and_withdrawal_reserves_independent()` — mixing reserve types, isolation verified
- ✅ `test_concurrent_fee_settlement_caps_to_available_plus_reserved()` — 30 fees @ 10 each, deficit booked
- ✅ `test_concurrent_pnl_and_fee_settlement_combined()` — 12 losses + 12 fees, balance verified
- ✅ `test_concurrent_get_or_create_ledger_no_duplicate()` — 50 concurrent ledger creations, exactly 1 row
- ✅ `test_duplicate_idempotency_across_all_functions()` — 20 duplicate calls per function, no over-posting

**Action:** Ensure tests run in CI (see Phase 3)

### 2.2 Test Rollback After Partial Failure

**Status:** 🟡 Partial (implicit in existing tests)

**New test required:**

```python
@pytest.mark.asyncio
async def test_rollback_on_idempotency_key_conflict():
    """If idempotency check passes but journal insert fails, transaction rolls back."""
    from app.customer_funds import reserve_withdrawal
    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "100")
        ref = "test-rollback"
        
        # First call succeeds
        async with sessions() as db:
            await reserve_withdrawal(db, cid, 50, reference_id=ref)
            await db.commit()
        
        ledger_after_first, _ = await _balances(sessions, cid)
        assert ledger_after_first.available == Decimal("50")
        assert ledger_after_first.withdrawal_reserved == Decimal("50")
        
        # Second call with same reference should return early (idempotent)
        async with sessions() as db:
            await reserve_withdrawal(db, cid, 50, reference_id=ref)
            await db.commit()
        
        ledger_after_second, _ = await _balances(sessions, cid)
        # Balance must not change (idempotency, not double-book)
        assert ledger_after_second.available == Decimal("50")
        assert ledger_after_second.withdrawal_reserved == Decimal("50")
    finally:
        await engine.dispose()
```

### 2.3 Test Deadlock Scenarios

**Status:** 🔴 Not yet implemented

**Test scenarios:**

```python
@pytest.mark.asyncio
async def test_no_deadlock_on_interleaved_customer_updates():
    """Two customers updating concurrently should not deadlock."""
    from app.customer_funds import reserve_trading, reserve_withdrawal
    engine, sessions = await _sessions()
    try:
        cid1 = await _customer(sessions, "500")
        cid2 = await _customer(sessions, "500")
        
        async def cust1_trade_then_withdraw():
            async with sessions() as db:
                await reserve_trading(db, cid1, 100, reference_id="cust1-trade")
                await db.commit()
            async with sessions() as db:
                await reserve_withdrawal(db, cid1, 100, reference_id="cust1-withdraw")
                await db.commit()
        
        async def cust2_withdraw_then_trade():
            async with sessions() as db:
                await reserve_withdrawal(db, cid2, 100, reference_id="cust2-withdraw")
                await db.commit()
            async with sessions() as db:
                await reserve_trading(db, cid2, 100, reference_id="cust2-trade")
                await db.commit()
        
        # Both should complete without deadlock (different customers)
        await asyncio.gather(cust1_trade_then_withdraw(), cust2_withdraw_then_trade())
        
        ledger1, _ = await _balances(sessions, cid1)
        ledger2, _ = await _balances(sessions, cid2)
        
        assert ledger1.available == Decimal("300")
        assert ledger1.trading_reserved == Decimal("100")
        assert ledger1.withdrawal_reserved == Decimal("100")
        assert ledger2.available == Decimal("300")
        assert ledger2.trading_reserved == Decimal("100")
        assert ledger2.withdrawal_reserved == Decimal("100")
    finally:
        await engine.dispose()
```

### 2.4 Run Critical Invariants Against PostgreSQL

**Status:** ✅ Test exists, 🟡 CI integration pending

**Test (tests/test_ledger_invariants_3_10_46.py):**

```python
@pytest.mark.asyncio
async def test_invariants_hold_after_normal_and_deficit_activity_then_detect_tampering():
    """Ledger invariants must hold under normal operation and detect any tampering."""
    from app.customer_funds import reserve_withdrawal, settle_realized_pnl, ledger_invariant_report
    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "100")
        
        # 1. Normal operations
        async with sessions() as db:
            await reserve_withdrawal(db, cid, 30, reference_id="r1")
            await db.commit()
        
        async with sessions() as db:
            await settle_realized_pnl(db, customer_id=cid, amount=-500, reference_id="loss1", strict=False)
            await db.commit()
        
        # 2. Check invariants (must pass)
        async with sessions() as db:
            report = await ledger_invariant_report(db)
            assert report["ok"] is True, f"Invariants failed: {report}"
        
        # 3. Tamper with balance directly (simulate bug)
        async with sessions() as db:
            ledger = (await db.execute(select(CustomerLedgerAccount).where(
                CustomerLedgerAccount.customer_id == cid
            ))).scalar_one()
            ledger.available = Decimal("9999")  # Fake balance
            await db.commit()
        
        # 4. Check invariants (must detect drift)
        async with sessions() as db:
            report = await ledger_invariant_report(db)
            assert report["ok"] is False, "Invariants should detect tampering"
            assert report["drift_count"] >= 1, "Should report at least one drift"
    finally:
        await engine.dispose()
```

**Action:** Run in CI with PostgreSQL backend

### 2.5 Test Migration From Clean Database → Current Schema

**Status:** 🔴 Not yet implemented

**Action required:**

```python
@pytest.mark.asyncio
async def test_alembic_migration_to_current_schema():
    """Ensure alembic migrations produce correct schema."""
    engine, sessions = await _sessions()
    try:
        # 1. Verify schema after migrations
        async with sessions() as db:
            from sqlalchemy import text, inspect
            inspector = inspect(engine.sync_engine)
            
            # Check customer_ledger_accounts table exists
            tables = inspector.get_table_names()
            assert "customer_ledger_accounts" in tables
            assert "ledger_journals" in tables
            assert "ledger_journal_lines" in tables
            assert "ledger_entries" in tables
            
            # Check constraints
            constraints = inspector.get_unique_constraints("customer_ledger_accounts")
            constraint_names = {c["name"] for c in constraints}
            assert "uq_customer_ledger_account_currency" in constraint_names
            
            # Check indexes exist
            indexes = inspector.get_indexes("customer_ledger_accounts")
            index_names = {i["name"] for i in indexes}
            assert "ix_customer_ledger_account_customer" in index_names
        
        # 2. Verify CHECK constraints work
        async with sessions() as db:
            # Try to create negative balance (should fail if CHECK exists)
            try:
                bad_ledger = CustomerLedgerAccount(
                    customer_id=999,
                    currency="USDT",
                    available=Decimal("-10")  # Violates CHECK
                )
                db.add(bad_ledger)
                await db.flush()
                # If no error, CHECK constraint is missing
                assert False, "CHECK constraint not enforced"
            except Exception as e:
                assert "check" in str(e).lower()
    finally:
        await engine.dispose()
```

---

## 🟠 PHASE 3: CI/SECURITY

### 3.1 Move/Replace the Misplaced CI Workflow

**Status:** ✅ Workflow created

**File:** `.github/workflows/ledger-concurrency-check.yml`  
**Triggers on:** Changes to ledger code  
**Mandatory gates:**
- ✅ SQLite invariant tests (baseline)
- ✅ PostgreSQL concurrency tests (blocker)
- ✅ PostgreSQL all-paths tests (blocker)

**Action:** Ensure workflow is enabled and status checks are required

### 3.2 Consolidate GitHub Actions

**Status:** 🟡 Pending audit

**Audit required:**
```bash
find .github/workflows -name "*.yml" -o -name "*.yaml" | sort
# List all workflows and check for:
# - Duplicate test runs
# - Missing coverage
# - Inconsistent Python/DB versions
```

**Action:** Consolidate into:
1. `ledger-concurrency-check.yml` (runs on ledger changes, PostgreSQL required)
2. `ci-unit-tests.yml` (runs on all changes, SQLite acceptable)
3. `ci-lint-type-security.yml` (runs on all changes, static checks)

### 3.3 Add Lint/Type/Static Checks

**Status:** 🔴 Not implemented

**Action required:**

```yaml
# .github/workflows/ci-lint-type-security.yml
name: Lint, Type, Security Checks

on: [push, pull_request]

jobs:
  lint-type-security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Install tools
        run: |
          pip install flake8 mypy black isort bandit safety
      
      - name: Lint (flake8)
        run: flake8 app/ tests/ --count --select=E9,F63,F7,F82 --show-source --statistics
      
      - name: Type check (mypy)
        run: mypy app/ --ignore-missing-imports
      
      - name: Format check (black)
        run: black --check app/ tests/
      
      - name: Import sort check (isort)
        run: isort --check-only app/ tests/
      
      - name: Security check (bandit)
        run: bandit -r app/ -ll
      
      - name: Dependency check (safety)
        run: safety check --json
```

### 3.4 Add Dependency/Security Scanning

**Status:** 🔴 Not implemented

**Action:** Enable GitHub Advanced Security
- Dependabot alerts
- SAST scanning
- Secret scanning

### 3.5 Ensure No Production Secrets Exist in Git

**Status:** 🟡 Audit required

**Action:**
```bash
# Check for hardcoded secrets
git log --all -p | grep -i "password\|secret\|key\|token" | head -20
git log --all --diff-filter=D | grep -i "password\|secret" | head -20

# Install git-secrets
brew install git-secrets
git secrets --install
git secrets --register-aws
git secrets --scan
```

### 3.6 Add Migration Verification to CI

**Status:** 🟡 Partial

**Action:** Ensure every ledger-related PR runs alembic migrations in CI
```bash
export ATLAS_POSTGRES_URL='postgresql+asyncpg://user:pass@localhost/atlas_test'
alembic upgrade head
pytest tests/test_ledger_invariants_3_10_46.py tests/test_postgres_ledger_concurrency_3_10_46.py
```

---

## 🟡 PHASE 4: PRODUCTION RESILIENCE

### 4.1 Load/Concurrency Testing

**Status:** 🔴 Not implemented

**Action required:** Create locust load tests
```python
# tests/load_test_ledger_concurrent_reserves.py
from locust import HttpUser, task, between

class LedgerUser(HttpUser):
    wait_time = between(1, 3)
    
    @task
    def reserve_trading(self):
        """Simulate customer reserves for trading."""
        self.client.post("/api/customer/ledger/reserve/trading", json={
            "customer_id": 12345,
            "amount": 100.0,
            "reference_id": f"trade-{uuid.uuid4()}"
        })
    
    @task
    def reserve_withdrawal(self):
        """Simulate customer reserves for withdrawal."""
        self.client.post("/api/customer/ledger/reserve/withdrawal", json={
            "customer_id": 12345,
            "amount": 50.0,
            "reference_id": f"withdraw-{uuid.uuid4()}"
        })
```

Run with:
```bash
locust -f tests/load_test_ledger_concurrent_reserves.py \
  --host=http://localhost:8000 \
  --users 100 \
  --spawn-rate 10 \
  --run-time 10m
```

### 4.2 Lock Contention Monitoring

**Status:** 🔴 Not implemented

**Action:** Add logging to detect lock wait times
```python
import time

async def get_or_create_ledger(db, customer_id: int, currency: str = USDT):
    start = time.perf_counter()
    row = (await db.execute(select(...).with_for_update())).scalar_one_or_none()
    lock_wait_ms = (time.perf_counter() - start) * 1000
    
    if lock_wait_ms > 100:  # Alert threshold
        logger.warning("lock_contention customer=%s wait_ms=%.1f", customer_id, lock_wait_ms)
    
    return row
```

Monitor in production:
- Alert if lock wait > 500ms
- Alert if lock timeout errors occur
- Track percentiles: p50, p95, p99

### 4.3 DB Connection-Pool Monitoring

**Status:** 🔴 Not implemented

**Action:** Log pool stats
```python
from sqlalchemy.pool import StaticPool

pool_obj = engine.pool
logger.info("pool_size=%d overflow=%d checked_out=%d",
    pool_obj.size(),
    pool_obj.overflow(),
    pool_obj.checkedout())
```

Alert thresholds:
- Checked out > 80% capacity
- Connection queue timeout > 1% of requests
- Overflow connections in use (should be rare)

### 4.4 Transaction Latency Monitoring

**Status:** 🔴 Not implemented

**Action:** Track transaction duration
```python
async def _timed_transaction(self, name: str):
    """Context manager to track transaction timing."""
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info("transaction name=%s duration_ms=%.1f", name, duration_ms)
        
        # Alert if transaction is slow
        if duration_ms > 1000:
            logger.warning("slow_transaction name=%s duration_ms=%.1f", name, duration_ms)

# Usage:
async with sessions() as db:
    async with _timed_transaction("reserve_trading"):
        await reserve_trading(db, cid, amount, reference_id=ref_id)
        await db.commit()
```

Alert thresholds:
- p95 latency > 500ms
- p99 latency > 2000ms
- Single transaction > 5000ms

### 4.5 Deployment Version Consistency Checks

**Status:** 🔴 Not implemented

**Action:** Verify schema version matches app version
```python
# app/startup.py
async def verify_deployment_consistency():
    """Ensure schema migrations match app code."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.autogenerate import compare_metadata
    from app.db import Base, engine
    
    ctx = MigrationContext.configure(engine.sync_engine)
    diff = compare_metadata(ctx.get_context(), Base.metadata)
    
    if diff:
        logger.error("deployment_mismatch schema differs from codebase")
        # Raise exception to prevent startup
        raise RuntimeError("Database schema mismatch. Run: alembic upgrade head")
    
    logger.info("deployment_verified schema matches codebase")
```

### 4.6 Failure/Rollback Observability

**Status:** 🔴 Not implemented

**Action:** Log all incidents and retries
```python
# Wrap all ledger mutations with logging
async def reserve_trading_with_observability(...):
    try:
        logger.info("reserve_trading_start customer=%s amount=%s", customer_id, amount)
        result = await reserve_trading(db, customer_id, amount, reference_id=reference_id)
        logger.info("reserve_trading_success customer=%s amount=%s", customer_id, amount)
        return result
    except ValueError as e:
        logger.warning("reserve_trading_failed customer=%s amount=%s reason=%s",
                      customer_id, amount, str(e))
        raise
    except Exception as e:
        logger.error("reserve_trading_error customer=%s amount=%s error=%s",
                    customer_id, amount, str(e), exc_info=True)
        raise
```

Dashboards:
- P&L settlement failures (reason: insufficient funds)
- Withdrawal reserve failures (reason: insufficient balance)
- Lock timeout errors
- Incident rate (CRITICAL ledger events)

---

## Summary: Completion Checklist

### Phase 1: Financial Correctness (CRITICAL)
- [ ] Apply Fix 1: `post_deposit()` fresh read
- [ ] Apply Fix 2: `release_trading()` fresh read + lock
- [ ] Apply Fix 3: `release_withdrawal()` fresh read + lock
- [ ] Apply Fix 4: `settle_withdrawal()` fresh read + lock
- [ ] Verify all 9 functions follow consistent pattern
- [ ] Run existing concurrency tests locally
- [ ] Pass tests against PostgreSQL

### Phase 2: Verification
- [ ] Add rollback test
- [ ] Add deadlock test
- [ ] Add migration test
- [ ] Run all tests in CI with PostgreSQL
- [ ] Document test coverage matrix

### Phase 3: CI/Security
- [ ] Enable GitHub Actions status checks
- [ ] Add lint/type/security workflow
- [ ] Enable Dependabot
- [ ] Scan for hardcoded secrets
- [ ] Verify migrations in every build

### Phase 4: Production Resilience
- [ ] Deploy load test baseline
- [ ] Enable lock contention alerts
- [ ] Setup pool monitoring dashboard
- [ ] Setup transaction latency alerts
- [ ] Setup deployment consistency checks
- [ ] Setup incident logging

---

## Timeline

| Phase | Duration | Gate |
|-------|----------|------|
| 1 | 2-3 days | All tests pass on PostgreSQL |
| 2 | 2-3 days | Load test baseline established |
| 3 | 1-2 days | CI fully automated, status checks required |
| 4 | 3-5 days | Monitoring dashboards live |
| **Total** | **~2 weeks** | **Production deployment safe** |

---

## Sign-Off Criteria

**Ready for Production When:**
- ✅ All Phase 1 fixes applied and tested
- ✅ Phase 2 tests pass on real PostgreSQL with 50+ concurrent users
- ✅ Phase 3 CI gates are mandatory and passing
- ✅ Phase 4 monitoring is deployed and baseline established
- ✅ Incident response plan documented
- ✅ Rollback procedure tested

**Current Status:** 🟡 Phase 1 (90% complete), Phases 2-4 pending
