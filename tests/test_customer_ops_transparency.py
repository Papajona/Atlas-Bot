"""Behavioral tests for authenticated customer ledger exports and trade explanations."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.main as main


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    def __init__(self, *, trade=None, entries=()):
        self.trade = trade
        self.entries = list(entries)
        self.executed = []
        self.mutations = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _Result(self.trade)

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def rollback(self):
        return None

    async def close(self):
        return None

    def add(self, *_args, **_kwargs):
        self.mutations.append("add")
        raise AssertionError("read-only customer export may not mutate database rows")

    async def commit(self):
        self.mutations.append("commit")
        raise AssertionError("read-only customer export may not commit")


def test_customer_csv_export_serializes_journal_lines_for_authenticated_customer(monkeypatch):
    db = _DB(entries=[{
        "created_at": "2026-10-20T10:00:00+00:00",
        "journal_id": 42,
        "entry_type": "DEPOSIT",
        "reference_type": "TRON_DEPOSIT",
        "reference_id": "tx-abc",
        "account_code": "LIABILITY:CUSTOMER:AVAILABLE",
        "debit": "0.000000",
        "credit": "12.500000",
    }])
    profile = SimpleNamespace(id=23)

    monkeypatch.setattr(main, "SessionLocal", lambda: db)
    monkeypatch.setattr(main, "get_customer", _async_value((profile, {"sub": "customer-23"})))

    async def export_bounds(fake_db, customer_id, currency):
        assert fake_db is db
        assert (customer_id, currency) == (23, "USDT")
        return 1, 42

    async def statement(fake_db, customer_id, currency, limit, *, offset, max_journal_id):
        assert fake_db is db
        assert (customer_id, currency, limit, offset, max_journal_id) == (23, "USDT", 1000, 0, 42)
        return db.entries[offset:offset + limit]

    monkeypatch.setattr(main, "ledger_statement_export_bounds", export_bounds)
    monkeypatch.setattr(main, "ledger_statement", statement)

    response = asyncio.run(main.customer_ledger_export_csv(authorization="opaque-token"))
    body = _stream_body(response)

    assert response.status_code == 200
    assert response.media_type.startswith("text/csv")
    assert 'filename="atlas-customer-ledger-usdt.csv"' in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    rows = body.decode("utf-8").splitlines()
    assert rows[0] == "created_at,journal_id,entry_type,currency,reference_type,reference_id,account_code,debit,credit"
    assert rows[1] == "2026-10-20T10:00:00+00:00,42,DEPOSIT,USDT,TRON_DEPOSIT,tx-abc,LIABILITY:CUSTOMER:AVAILABLE,0.000000,12.500000"
    assert not db.mutations


def _async_value(value):
    async def inner(*_args, **_kwargs):
        return value
    return inner


def _stream_body(response):
    async def collect():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
        return b"".join(chunks)

    return asyncio.run(collect())


def test_customer_trade_decision_is_owner_scoped_and_reports_only_persisted_reason(monkeypatch):
    now = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
    trade = SimpleNamespace(
        id=88, customer_id=23, symbol="BTC/USDT", side="buy",
        mode="PAPER", status="SIMULATED", signal_id="sig-88",
        reason='{"risk_gate":"passed"}', error="", created_at=now, updated_at=now,
    )
    db = _DB(trade=trade)
    profile = SimpleNamespace(id=23)
    monkeypatch.setattr(main, "SessionLocal", lambda: db)
    monkeypatch.setattr(main, "get_customer", _async_value((profile, {"sub": "customer-23"})))

    result = asyncio.run(main.customer_trade_decision(88, authorization="opaque-token"))

    assert result["trade_id"] == 88
    assert result["mode"] == "PAPER"
    assert result["reason"] == '{"risk_gate":"passed"}'
    assert result["explanation_scope"] == "stored_trade_record"
    assert "does not reconstruct decisions not recorded" in result["note"]
    assert len(db.executed) == 1
    where = str(db.executed[0].whereclause)
    assert "trades.id" in where
    assert "trades.customer_id" in where
    assert not db.mutations


def test_customer_trade_decision_hides_another_customer_trade_as_not_found(monkeypatch):
    db = _DB(trade=None)
    profile = SimpleNamespace(id=23)
    monkeypatch.setattr(main, "SessionLocal", lambda: db)
    monkeypatch.setattr(main, "get_customer", _async_value((profile, {"sub": "customer-23"})))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.customer_trade_decision(88, authorization="opaque-token"))

    assert exc.value.status_code == 404
    assert str(db.executed[0].whereclause).count("trades.customer_id") == 1
    assert not db.mutations


def test_customer_csv_export_includes_more_than_5000_journal_lines(monkeypatch):
    entries = [
        {
            "created_at": "2026-11-02T10:00:00+00:00",
            "journal_id": index,
            "entry_type": "DEPOSIT",
            "reference_type": "TRON_DEPOSIT",
            "reference_id": f"tx-{index}",
            "account_code": "LIABILITY:CUSTOMER:AVAILABLE",
            "debit": "0.000000",
            "credit": "1.000000",
        }
        for index in range(5001)
    ]
    db = _DB(entries=entries)
    profile = SimpleNamespace(id=23)
    monkeypatch.setattr(main, "SessionLocal", lambda: db)
    monkeypatch.setattr(main, "get_customer", _async_value((profile, {"sub": "customer-23"})))

    pages = []

    async def export_bounds(fake_db, customer_id, currency):
        assert fake_db is db
        assert (customer_id, currency) == (23, "USDT")
        return len(entries), 5000

    async def statement(fake_db, customer_id, currency, limit, *, offset, max_journal_id):
        assert fake_db is db
        assert (customer_id, currency, max_journal_id) == (23, "USDT", 5000)
        pages.append((offset, limit))
        return entries[offset:offset + limit]

    monkeypatch.setattr(main, "ledger_statement_export_bounds", export_bounds)
    monkeypatch.setattr(main, "ledger_statement", statement)
    response = asyncio.run(main.customer_ledger_export_csv(authorization="opaque-token"))

    rows = _stream_body(response).decode("utf-8").splitlines()
    assert response.status_code == 200
    assert len(rows) == 5002  # header plus all 5,001 journal lines
    assert rows[-1].split(",")[1] == "5000"
    assert response.headers["X-Atlas-Export-Row-Count"] == "5001"
    assert response.headers["X-Atlas-Export-Truncated"] == "false"
    assert pages == [(0, 1000), (1000, 1000), (2000, 1000), (3000, 1000), (4000, 1000), (5000, 1000)]
