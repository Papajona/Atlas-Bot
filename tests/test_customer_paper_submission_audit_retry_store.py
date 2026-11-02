"""Regression tests for indexed, isolated customer paper audit retries."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


import pytest
from sqlalchemy.dialects import postgresql, sqlite

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
    assert captured["committed"] is True



def test_retry_store_migration_enables_row_level_security(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    migration_path = root / "alembic" / "versions" / "0056_customer_paper_submission_audit_retries.py"
    spec = importlib.util.spec_from_file_location("migration_0056_customer_paper_submission_audit_retries", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    executed = []
    monkeypatch.setattr(migration.op, "create_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration.op, "create_index", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(migration.op, "execute", lambda statement: executed.append(str(statement)))

    migration.upgrade()

    assert any(
        "ALTER TABLE public.customer_paper_submission_audit_retries ENABLE ROW LEVEL SECURITY" in statement
        for statement in executed
    )
