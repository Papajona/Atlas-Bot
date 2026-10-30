from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import main


def _pending_execution(*, newly_submitted: bool = True) -> dict:
    return {
        "status": "WAITING_FOR_NEXT_COMPLETED_BAR",
        "decision": "NO_TRADE",
        "execution": {
            "status": "WAITING_FOR_NEXT_COMPLETED_BAR",
            "order_id": 44021,
        },
        "order_intent": {
            "client_order_id": "cp-regression-order",
            "order_id": 44021,
            "side": "BUY",
            "quantity": "0.250000000000000000",
            "bar_timestamp": "2026-10-30T10:00:00+00:00",
            "newly_submitted": newly_submitted,
        },
    }


def test_new_pending_paper_order_has_distinct_stage_and_submission_audit(monkeypatch):
    events = []

    async def capture(event, detail):
        events.append((event, detail))

    monkeypatch.setattr(main, "_safe_audit", capture)
    req = SimpleNamespace(
        symbol="BTC/USDT",
        exchange="binance",
        timeframe="1h",
    )

    stage = asyncio.run(main._customer_paper_submission_stage(
        _pending_execution(),
        customer_id=501,
        trading_account_id=702,
        req=req,
    ))

    assert stage == "customer_paper_order_submitted"
    assert len(events) == 1
    event, detail = events[0]
    assert event == "CUSTOMER_PAPER_ORDER_SUBMITTED"
    assert detail == {
        "customer_id": 501,
        "trading_account_id": 702,
        "symbol": "BTC/USDT",
        "exchange": "binance",
        "timeframe": "1h",
        "client_order_id": "cp-regression-order",
        "order_id": 44021,
        "side": "BUY",
        "quantity": "0.250000000000000000",
        "bar_timestamp": "2026-10-30T10:00:00+00:00",
        "status": "WAITING_FOR_NEXT_COMPLETED_BAR",
    }


def test_replayed_pending_order_is_not_audited_as_a_new_submission(monkeypatch):
    events = []

    async def capture(event, detail):
        events.append((event, detail))

    monkeypatch.setattr(main, "_safe_audit", capture)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        _pending_execution(newly_submitted=False),
        customer_id=501,
        trading_account_id=702,
        req=req,
    ))

    assert stage == "customer_paper_order_pending"
    assert events == []


def test_non_pending_execution_does_not_change_its_result_stage(monkeypatch):
    events = []

    async def capture(event, detail):
        events.append((event, detail))

    monkeypatch.setattr(main, "_safe_audit", capture)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        {"status": "APPENDED", "decision": "TRADE"},
        customer_id=501,
        trading_account_id=702,
        req=req,
    ))

    assert stage is None
    assert events == []


def test_both_customer_paper_trade_paths_use_pending_submission_stage():
    from pathlib import Path

    source = Path(main.__file__).read_text(encoding="utf-8")
    assert source.count("pending_stage = await _customer_paper_submission_stage(") == 2
    assert '"stage": pending_stage' in source
