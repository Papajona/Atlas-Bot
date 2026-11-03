"""Regression tests for indexed, isolated customer paper audit retries."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.sql.elements import BinaryExpression, BindParameter
from sqlalchemy.sql.visitors import iterate

from app import main


def test_retry_loader_is_customer_scoped_and_quarantines_one_corrupt_ciphertext(monkeypatch):
    customer_id = 711
    good_detail = {
        "customer_id": customer_id,
        "trading_account_id": 92,
        "client_order_id": "retry-good-order",
        "symbol": "BTC/USDT",
        "exchange": "binance",
        "timeframe": "1h",
        "status": "SUBMITTED",
    }
    rows = [
        SimpleNamespace(id=10, client_order_id="retry-corrupt-order", raw_detail="corrupt-ciphertext"),
        SimpleNamespace(id=11, client_order_id="retry-good-order", raw_detail=json.dumps(good_detail)),
    ]
    captured = {}
    quarantined = []

    class Result:
        def all(self):
            return rows

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            captured["sql"] = str(statement)
            return Result()

    def decrypt(value):
        if value == "corrupt-ciphertext":
            raise RuntimeError("ciphertext detail must not be logged")
        return value

    async def set_state(retry_id, *, status, error_type="", increment_attempts=False):
        quarantined.append((retry_id, status, error_type, increment_attempts))

    monkeypatch.setattr(main, "SessionLocal", lambda: Session())
    monkeypatch.setattr(main, "decrypt_text", decrypt)
    monkeypatch.setattr(main, "_set_customer_paper_submission_audit_retry_state", set_state)

    pending = asyncio.run(main._load_customer_paper_submission_audit_retries(customer_id))

    assert len(pending) == 1
    assert pending[0]["retry_id"] == 11
    assert pending[0]["client_order_id"] == "retry-good-order"
    assert pending[0]["submission_detail"] == good_detail
    assert quarantined == [(10, "CORRUPT", "RuntimeError", True)]

    sql = captured["sql"]
    assert "customer_paper_submission_audit_retries" in sql
    assert "customer_id" in sql
    assert "status" in sql
    assert "audit_log" not in sql
    assert "LIMIT" in sql.upper()


def test_retry_repair_continues_after_one_order_fails_and_logs_safe_traceback(monkeypatch, caplog):
    pending = [
        {
            "retry_id": 20,
            "customer_id": 712,
            "client_order_id": "retry-first-fails",
            "submission_detail": {"customer_id": 712, "client_order_id": "retry-first-fails"},
        },
        {
            "retry_id": 21,
            "customer_id": 712,
            "client_order_id": "retry-second-succeeds",
            "submission_detail": {"customer_id": 712, "client_order_id": "retry-second-succeeds"},
        },
    ]
    replayed = []
    states = []

    async def load(_customer_id):
        return pending

    async def audit_once(detail):
        replayed.append(detail["client_order_id"])
        if detail["client_order_id"] == "retry-first-fails":
            raise RuntimeError("do not expose this internal text")

    async def set_state(retry_id, *, status, error_type="", increment_attempts=False):
        states.append((retry_id, status, error_type, increment_attempts))

    monkeypatch.setattr(main, "_load_customer_paper_submission_audit_retries", load)
    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    monkeypatch.setattr(main, "_set_customer_paper_submission_audit_retry_state", set_state)

    asyncio.run(main._repair_customer_paper_submission_audit_retries(712))

    assert replayed == ["retry-first-fails", "retry-second-succeeds"]
    assert (20, "PENDING", "RuntimeError", True) in states
    assert (21, "REPAIRED", "", False) in states
    assert "customer_paper_submission_audit_retry_failed" in caplog.text
    assert "retry-first-fails" in caplog.text
    assert "traceback_frames=" in caplog.text
    assert "do not expose this internal text" not in caplog.text


def test_terminal_submission_audit_failure_persists_retry_without_changing_fill_result(monkeypatch):
    captured = []
    fallback_events = []

    async def no_prior_retries(_customer_id):
        return None

    async def fail_audit(_detail):
        raise RuntimeError("audit store unavailable")

    async def persist_retry(**kwargs):
        captured.append(kwargs)

    async def safe_audit(event, detail, actor_id=None):
        fallback_events.append((event, detail, actor_id))

    monkeypatch.setattr(main, "_repair_customer_paper_submission_audit_retries", no_prior_retries)
    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", fail_audit)
    monkeypatch.setattr(main, "_persist_customer_paper_submission_audit_retry", persist_retry)
    monkeypatch.setattr(main, "_safe_audit", safe_audit)

    execution = {
        "status": "APPENDED",
        "decision": "TRADE",
        "order_intent": {
            "client_order_id": "terminal-retry-order",
            "order_id": "order-991",
            "side": "BUY",
            "quantity": "0.25",
            "bar_timestamp": "2026-11-02T10:00:00+00:00",
            "submission_status": "WAITING_FOR_FILL_LATENCY",
            "execution_status": "APPENDED",
            "order_role": "STRATEGY",
            "newly_submitted": False,
        },
    }
    request = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        execution,
        customer_id=712,
        trading_account_id=92,
        req=request,
    ))

    assert stage is None
    assert execution["decision"] == "TRADE"
    assert execution["status"] == "APPENDED"
    assert captured[0]["customer_id"] == 712
    assert captured[0]["submission_detail"]["client_order_id"] == "terminal-retry-order"
    assert captured[0]["error_type"] == "RuntimeError"
    assert fallback_events[0][0] == "CUSTOMER_PAPER_ORDER_SUBMISSION_AUDIT_INCOMPLETE"
    assert fallback_events[0][2] == "customer:712"

@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_retry_state_transition_binds_pending_predicate_explicitly(monkeypatch, dialect):
    captured = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            compiled = statement.compile(dialect=dialect)
            captured["statement"] = statement
            captured["sql"] = str(compiled)
            captured["params"] = compiled.params
            captured["bind_names"] = compiled.bind_names

        async def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(main, "SessionLocal", lambda: FakeSession())
    asyncio.run(main._set_customer_paper_submission_audit_retry_state(
        714,
        status="PENDING",
        error_type="RuntimeError",
        increment_attempts=True,
    ))

    statement = captured["statement"]
    sql = captured["sql"]
    assert "WHERE customer_paper_submission_audit_retries.id =" in sql
    assert "AND customer_paper_submission_audit_retries.status =" in sql
    normalized_sql = "".join(sql.split())
    assert "attempt_count=CASEWHEN" in normalized_sql
    assert "ELSEcustomer_paper_submission_audit_retries.attempt_count+" in normalized_sql
    assert "CORRUPT" in captured["params"]

    status_predicates = [
        node for node in iterate(statement.whereclause)
        if isinstance(node, BinaryExpression)
        and getattr(node.left, "key", None) == "status"
    ]
    assert len(status_predicates) == 1
    predicate_value = status_predicates[0].right
    assert isinstance(predicate_value, BindParameter)
    predicate_parameter_name = captured["bind_names"][predicate_value]
    assert captured["params"][predicate_parameter_name] == "PENDING"
    assert captured["committed"] is True


def test_retry_state_failure_from_stale_worker_cannot_reopen_repaired_row(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE customer_paper_submission_audit_retries (
                id INTEGER PRIMARY KEY,
                status VARCHAR(20) NOT NULL,
                attempt_count INTEGER NOT NULL,
                last_error_type VARCHAR(80) NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            text(
                """
                INSERT INTO customer_paper_submission_audit_retries
                    (id, status, attempt_count, last_error_type, updated_at)
                VALUES
                    (1, 'PENDING', 1, 'OldError', '2026-11-02 10:00:00'),
                    (2, 'REPAIRED', 1, '', '2026-11-02 10:00:00')
                """
            )
        )

    class SQLiteSession:
        async def __aenter__(self):
            self.connection = engine.connect()
            return self

        async def __aexit__(self, *_args):
            self.connection.close()
            return False

        async def execute(self, statement):
            return self.connection.execute(statement)

        async def commit(self):
            self.connection.commit()

    monkeypatch.setattr(main, "SessionLocal", lambda: SQLiteSession())
    asyncio.run(main._set_customer_paper_submission_audit_retry_state(
        2,
        status="PENDING",
        error_type="LateWorkerError",
        increment_attempts=True,
    ))
    asyncio.run(main._set_customer_paper_submission_audit_retry_state(
        1,
        status="PENDING",
        error_type="RetryError",
        increment_attempts=True,
    ))

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, status, attempt_count, last_error_type
                FROM customer_paper_submission_audit_retries
                ORDER BY id
                """
            )
        ).all()
    engine.dispose()

    assert rows == [
        (1, "PENDING", 2, "RetryError"),
        (2, "REPAIRED", 1, ""),
    ]


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_retry_upsert_is_unique_by_customer_and_order_and_reopens_pending_state(monkeypatch, dialect):
    captured = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get_bind(self):
            return SimpleNamespace(dialect=dialect)

        async def execute(self, statement):
            captured["sql"] = str(statement.compile(dialect=dialect))
            captured["params"] = str(statement.compile(dialect=dialect).params)

        async def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(main, "SessionLocal", lambda: FakeSession())
    asyncio.run(main._persist_customer_paper_submission_audit_retry(
        customer_id=713,
        submission_detail={
            "customer_id": 713,
            "client_order_id": "stable-retry-order",
            "status": "SUBMITTED",
        },
        error_type="RuntimeError",
    ))

    sql = captured["sql"]
    assert "ON CONFLICT" in sql.upper()
    assert "customer_id" in sql
    assert "client_order_id" in sql
    assert "PENDING" in captured["params"]
    # Later recovery failures may reopen the retry and update diagnostics, but
    # must never replace the first immutable submission snapshot.
    update_clause = sql.split("DO UPDATE SET", 1)[1]
    assert "submission_detail" not in update_clause
    assert captured["committed"] is True



def test_retry_store_migration_enables_row_level_security(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    migration_path = root / "alembic" / "versions" / "0057_customer_paper_submission_audit_retries.py"
    spec = importlib.util.spec_from_file_location("migration_0057_customer_paper_submission_audit_retries", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    executed = []
    monkeypatch.setattr(migration.op, "create_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration.op, "create_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration.op, "execute", lambda statement: executed.append(str(statement)))
    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )

    migration.upgrade()

    assert any(
        "ALTER TABLE public.customer_paper_submission_audit_retries ENABLE ROW LEVEL SECURITY" in statement
        for statement in executed
    )

    statements = "\n".join(executed)
    assert "REVOKE ALL PRIVILEGES ON TABLE public.customer_paper_submission_audit_retries FROM PUBLIC" in statements
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE public.customer_paper_submission_audit_retries TO %I" in statements
    assert "REVOKE ALL PRIVILEGES ON SEQUENCE public.customer_paper_submission_audit_retries_id_seq FROM PUBLIC" in statements
    assert "GRANT USAGE, SELECT ON SEQUENCE public.customer_paper_submission_audit_retries_id_seq TO %I" in statements
    assert "has_table_privilege(r.rolname, 'public.audit_log', 'INSERT')" in statements
    assert "'service_role'" in statements

def test_retry_repair_defers_an_active_delivery_lease_without_spending_attempt(monkeypatch):
    pending = [{
        "retry_id": 31,
        "customer_id": 715,
        "client_order_id": "retry-held-by-competing-worker",
        "submission_detail": {
            "customer_id": 715,
            "client_order_id": "retry-held-by-competing-worker",
            "status": "SUBMITTED",
        },
    }]
    audit_attempts = []
    state_updates = []

    async def load(_customer_id):
        return pending

    async def audit_once(detail):
        audit_attempts.append(detail["client_order_id"])
        if len(audit_attempts) == 1:
            raise main.CustomerPaperSubmissionAuditDeliveryInFlight(
                "another worker owns the delivery lease"
            )
        return False  # The outbox is now DELIVERED; this is an idempotent replay.

    async def set_state(retry_id, *, status, error_type="", increment_attempts=False):
        state_updates.append((retry_id, status, error_type, increment_attempts))

    monkeypatch.setattr(main, "_load_customer_paper_submission_audit_retries", load)
    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    monkeypatch.setattr(main, "_set_customer_paper_submission_audit_retry_state", set_state)

    asyncio.run(main._repair_customer_paper_submission_audit_retries(715))
    assert state_updates == [
        (31, "PENDING", "CustomerPaperSubmissionAuditDeliveryInFlight", False)
    ]

    # A later repair cycle can retry once the competing lease has expired/failed.
    asyncio.run(main._repair_customer_paper_submission_audit_retries(715))
    assert audit_attempts == [
        "retry-held-by-competing-worker",
        "retry-held-by-competing-worker",
    ]
    assert state_updates[-1] == (31, "REPAIRED", "", False)

@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_active_lease_upsert_does_not_increment_and_preserves_corrupt_quarantine(monkeypatch, dialect):
    captured = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get_bind(self):
            return SimpleNamespace(dialect=dialect)

        async def execute(self, statement):
            compiled = statement.compile(dialect=dialect)
            captured["sql"] = str(compiled)
            captured["params"] = compiled.params

        async def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(main, "SessionLocal", lambda: FakeSession())
    asyncio.run(main._persist_customer_paper_submission_audit_retry(
        customer_id=716,
        submission_detail={
            "customer_id": 716,
            "client_order_id": "active-lease-retry",
            "status": "SUBMITTED",
        },
        error_type="CustomerPaperSubmissionAuditDeliveryInFlight",
        increment_attempts=False,
    ))

    update_clause = captured["sql"].split("DO UPDATE SET", 1)[1]
    assert "attempt_count" not in update_clause
    assert "submission_detail" not in update_clause
    assert "CASE" in update_clause
    assert "CORRUPT" in captured["params"]
    assert "PENDING" in captured["params"]
    assert captured["committed"] is True
