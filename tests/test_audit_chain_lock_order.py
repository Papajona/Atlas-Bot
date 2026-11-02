"""Audit hash-chain regression tests."""
import asyncio
import os
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.audit_chain import append_audit, append_audit_once
from app.config import settings
from app.db import AuditChainState, AuditLog, Base

@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")

def test_append_audit_refreshes_state_loaded_before_the_lock(tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'a.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        a, b = S(), S()
        try:
            await append_audit(a, event="E1", detail={}, actor_id="t")
            held = await a.get(AuditChainState, 1)
            second = await append_audit(b, event="E2", detail={}, actor_id="t")
            third = await append_audit(a, event="E3", detail={}, actor_id="t")
            assert third.previous_hash == second.event_hash
            async with S() as reader:
                rows = (await reader.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
                assert all(y.previous_hash == x.event_hash for x, y in zip(rows, rows[1:]))
            assert held is not None
        finally:
            await a.close()
            await b.close()
            await engine.dispose()
    asyncio.run(go())

def test_append_audit_once_recovers_missing_submission_event_without_duplicates(tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'audit-once.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        detail = {
            "customer_id": 99231,
            "client_order_id": "cp-stable-order-identity",
            "side": "BUY",
            "quantity": "0.25",
        }
        try:
            async with S() as first_db:
                first, created = await append_audit_once(
                    first_db,
                    event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                    detail=detail,
                    actor_id="customer:99231",
                    idempotency_fields=("customer_id", "client_order_id"),
                )
            # Corrupt only the encrypted detail payload. Retry de-duplication must
            # use the indexed identity columns and must not deserialize this row.
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE audit_log SET detail='damaged-ciphertext' WHERE id=:id"),
                    {"id": first.id},
                )
            async with S() as recovery_db:
                replay, created_again = await append_audit_once(
                    recovery_db,
                    event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                    detail=detail,
                    actor_id="customer:99231",
                    idempotency_fields=("customer_id", "client_order_id"),
                )
            async with S() as reader:
                rows = (
                    await reader.execute(
                        select(AuditLog.id).where(AuditLog.event == "CUSTOMER_PAPER_ORDER_SUBMITTED")
                    )
                ).scalars().all()
            assert created is True
            assert created_again is False
            assert replay.id == first.id
            assert len(rows) == 1
            assert "uq_audit_log_event_idempotency" in {
                index.name for index in AuditLog.__table__.indexes
            }
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_existing_idempotency_hit_does_not_consume_rate_limit(tmp_path, monkeypatch):
    import app.audit_chain as audit_chain

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'audit-no-rate-limit-replay.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        payload = {"customer_id": 503, "client_order_id": "cp-no-rate-limit-replay"}
        try:
            async with S() as db:
                first, created = await append_audit_once(
                    db,
                    event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                    detail=payload,
                    actor_id="customer:503",
                    idempotency_fields=("customer_id", "client_order_id"),
                )
            assert created is True

            async def fail_if_rate_limited(*_args, **_kwargs):
                raise AssertionError("an existing idempotent replay must not consume the audit rate limit")

            monkeypatch.setattr(audit_chain, "allow_rate_limit", fail_if_rate_limited)
            async with S() as db:
                replay, created_again = await append_audit_once(
                    db,
                    event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                    detail=payload,
                    actor_id="customer:503",
                    idempotency_fields=("customer_id", "client_order_id"),
                )
                assert not db.in_transaction(), "an idempotent replay must release its read transaction"
            assert created_again is False
            assert replay.id == first.id
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_existing_idempotency_hit_does_not_take_global_chain_lock(tmp_path, monkeypatch):
    import app.audit_chain as audit_chain

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'audit-fast-replay.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        payload = {"customer_id": 502, "client_order_id": "cp-fast-replay"}
        try:
            async with S() as db:
                first, created = await append_audit_once(
                    db,
                    event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                    detail=payload,
                    actor_id="customer:502",
                    idempotency_fields=("customer_id", "client_order_id"),
                )
            assert created is True

            async def fail_if_locked(_db):
                raise AssertionError("existing idempotent audit replay must not lock the global chain head")

            monkeypatch.setattr(audit_chain, "_lock_chain_state", fail_if_locked)
            async with S() as db:
                replay, created_again = await append_audit_once(
                    db,
                    event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                    detail=payload,
                    actor_id="customer:502",
                    idempotency_fields=("customer_id", "client_order_id"),
                )
            assert created_again is False
            assert replay.id == first.id
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_concurrent_same_identity_audit_retries_are_idempotent_on_sqlite(tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'audit-concurrent-retry.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        detail = {
            "customer_id": 99232,
            "client_order_id": "cp-concurrent-sqlite-identity",
            "side": "BUY",
            "quantity": "0.25",
        }

        async def append_once():
            async with S() as db:
                return await append_audit_once(
                    db,
                    event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                    detail=detail,
                    actor_id="customer:99232",
                    idempotency_fields=("customer_id", "client_order_id"),
                )

        try:
            results = await asyncio.gather(*(append_once() for _ in range(12)), return_exceptions=True)
            errors = [result for result in results if isinstance(result, Exception)]
            assert not errors
            successful = [result for result in results if not isinstance(result, Exception)]
            assert len(successful) == 12
            assert sum(1 for _row, created in successful if created) == 1
            assert len({row.id for row, _created in successful}) == 1

            async with S() as reader:
                rows = (
                    await reader.execute(
                        select(AuditLog.id, AuditLog.event_hash).where(
                            AuditLog.event == "CUSTOMER_PAPER_ORDER_SUBMITTED",
                            AuditLog.idempotency_key.is_not(None),
                        )
                    )
                ).all()
            assert len(rows) == 1
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_cross_process_sqlite_lock_retries_distinct_audit_identities(tmp_path, monkeypatch):
    import app.audit_chain as audit_chain

    async def go():
        database = tmp_path / "audit-cross-process.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database}",
            connect_args={"timeout": 0.01},
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        blocker = None
        try:
            async with S() as bootstrap:
                await append_audit(
                    bootstrap, event="CROSS_PROCESS_SQLITE_BOOTSTRAP",
                    detail={}, actor_id="test",
                )

            # A fresh lock per caller models independent worker processes: no
            # in-memory asyncio.Lock can coordinate these writers.
            monkeypatch.setattr(audit_chain, "_sqlite_audit_lock", lambda _db: asyncio.Lock())
            blocker = await engine.connect()
            blocker_tx = await blocker.begin()
            await blocker.execute(text("UPDATE audit_chain_state SET id = id WHERE id = 1"))

            async def append_distinct(index):
                async with S() as db:
                    detail = {
                        "customer_id": 99300 + index,
                        "client_order_id": f"cp-cross-process-{index}",
                    }
                    return await append_audit_once(
                        db,
                        event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                        detail=detail,
                        actor_id=f"customer:{99300 + index}",
                        idempotency_fields=("customer_id", "client_order_id"),
                    )

            tasks = [asyncio.create_task(append_distinct(index)) for index in range(8)]
            await asyncio.sleep(0.08)
            await blocker_tx.commit()
            await blocker.close()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [result for result in results if isinstance(result, Exception)]
            assert not errors, f"cross-process audit append errors: {[repr(error) for error in errors]}"
            successful = [result for result in results if not isinstance(result, Exception)]
            assert len(successful) == 8
            assert all(created for _row, created in successful)
            assert len({row.id for row, _created in successful}) == 8

            async with S() as reader:
                rows = (
                    await reader.execute(
                        select(AuditLog.id, AuditLog.previous_hash, AuditLog.event_hash)
                        .order_by(AuditLog.id)
                    )
                ).all()
                submitted = (
                    await reader.execute(
                        select(AuditLog.id).where(
                            AuditLog.event == "CUSTOMER_PAPER_ORDER_SUBMITTED"
                        )
                    )
                ).scalars().all()
            assert len(submitted) == 8
            assert all(later[1] == earlier[2] for earlier, later in zip(rows, rows[1:]))
        finally:
            if blocker is not None and not blocker.closed:
                await blocker.close()
            await engine.dispose()

    asyncio.run(go())


@pytest.mark.postgres
@pytest.mark.skipif(not os.getenv("ATLAS_POSTGRES_URL"), reason="needs migrated PostgreSQL (ATLAS_POSTGRES_URL)")
def test_concurrent_audit_writers_never_fork_the_chain_on_postgres():
    from app.audit_chain import record_audit
    async def _all():
        from app.db import SessionLocal
        async with SessionLocal() as db:
            return (await db.execute(select(AuditLog.id, AuditLog.previous_hash, AuditLog.event_hash).order_by(AuditLog.id))).all()
    async def go():
        rows_before = await _all()
        res = await asyncio.gather(*[record_audit("PG_PROBE", {"i": i}, "t") for i in range(25)], return_exceptions=True)
        assert not [r for r in res if isinstance(r, Exception)]
        added = (await _all())[len(rows_before):]
        assert len(added) == 25
        assert all(y[1] == x[2] for x, y in zip(added, added[1:])), "audit chain forked"
    asyncio.run(go())


def test_new_audit_releases_read_transaction_before_rate_limit(tmp_path, monkeypatch):
    import app.audit_chain as audit_chain

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'audit-release-connection.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with S() as db:
                observed = []

                async def rate_limit_after_release(detail, actor_id):
                    observed.append((db.in_transaction(), actor_id))
                    assert not db.in_transaction(), "Redis must run after the read transaction releases its connection"
                    return dict(detail)

                monkeypatch.setattr(audit_chain, "_rate_limited_detail", rate_limit_after_release)
                row, created = await append_audit_once(
                    db,
                    event="CUSTOMER_PAPER_ORDER_SUBMITTED",
                    detail={"customer_id": 504, "client_order_id": "cp-release-connection"},
                    actor_id="customer:504",
                    idempotency_fields=("customer_id", "client_order_id"),
                )
            assert created is True
            assert row.id is not None
            assert observed == [(False, "customer:504")]
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_audit_idempotency_migration_retry_recovers_invalid_concurrent_index(monkeypatch):
    import importlib.util
    from contextlib import contextmanager
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration_path = root / "alembic" / "versions" / "0054_audit_log_idempotency_key.py"
    spec = importlib.util.spec_from_file_location("migration_0054_audit_idempotency_key_test", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    class Result:
        def __init__(self, value):
            self.value = value

        def first(self):
            return self.value

    class Bind:
        def __init__(self):
            self.column_exists = False
            self.index_state = None
            self.executed = []

        def execute(self, statement):
            self.executed.append(str(statement))
            return Result(self.index_state)

    class Inspector:
        def __init__(self, bind):
            self.bind = bind

        def get_columns(self, table_name):
            assert table_name == "audit_log"
            return [{"name": "idempotency_key"}] if self.bind.column_exists else []

    class Context:
        class Dialect:
            name = "postgresql"

        dialect = Dialect()

        @contextmanager
        def autocommit_block(self):
            yield

    bind = Bind()
    calls = {"add_column": [], "create_index": [], "execute": []}
    monkeypatch.setattr(migration.op, "get_context", lambda: Context())
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: Inspector(bind))

    def add_column(table, column):
        calls["add_column"].append((table, column.name))
        bind.column_exists = True

    def create_index(name, table, columns, **kwargs):
        calls["create_index"].append((name, table, tuple(columns), kwargs))
        if len(calls["create_index"]) == 1:
            # Simulate a failed concurrent build leaving an invalid catalog entry.
            bind.index_state = (False, False)
            raise RuntimeError("simulated interrupted concurrent index build")
        bind.index_state = (True, True)

    def execute(statement):
        calls["execute"].append(str(statement))
        bind.index_state = None

    monkeypatch.setattr(migration.op, "add_column", add_column)
    monkeypatch.setattr(migration.op, "create_index", create_index)
    monkeypatch.setattr(migration.op, "execute", execute)

    with pytest.raises(RuntimeError, match="simulated interrupted concurrent index build"):
        migration.upgrade()

    assert bind.column_exists is True
    assert bind.index_state == (False, False)

    migration.upgrade()

    assert len(calls["add_column"]) == 1
    assert len(calls["create_index"]) == 2
    assert any(
        "DROP INDEX CONCURRENTLY IF EXISTS uq_audit_log_event_idempotency" in statement
        for statement in calls["execute"]
    )
    assert bind.index_state == (True, True)


def test_audit_idempotency_migration_reuses_valid_concurrent_index(monkeypatch):
    import importlib.util
    from contextlib import contextmanager
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration_path = root / "alembic" / "versions" / "0054_audit_log_idempotency_key.py"
    spec = importlib.util.spec_from_file_location("migration_0054_audit_log_idempotency_key_valid_test", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    class Result:
        def first(self):
            return (True, True)

    class Bind:
        def execute(self, _statement):
            return Result()

    class Inspector:
        def get_columns(self, table_name):
            assert table_name == "audit_log"
            return [{"name": "idempotency_key"}]

    class Context:
        class Dialect:
            name = "postgresql"

        dialect = Dialect()

        @contextmanager
        def autocommit_block(self):
            yield

    calls = {"create": [], "execute": []}
    monkeypatch.setattr(migration.op, "get_context", lambda: Context())
    monkeypatch.setattr(migration.op, "get_bind", lambda: Bind())
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: Inspector())
    monkeypatch.setattr(migration.op, "create_index", lambda *args, **kwargs: calls["create"].append((args, kwargs)))
    monkeypatch.setattr(migration.op, "execute", lambda statement: calls["execute"].append(str(statement)))

    migration.upgrade()

    assert calls == {"create": [], "execute": []}
