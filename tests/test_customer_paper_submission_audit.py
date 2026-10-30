from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import main


def _pending_execution(*, newly_submitted: bool = True) -> dict:
    return {
        "status": "WAITING_FOR_NEXT_COMPLETED_BAR",
        "decision": "NO_TRADE",
        "execution": {"status": "WAITING_FOR_NEXT_COMPLETED_BAR", "order_id": 44021},
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

    async def audit_once(detail):
        events.append(detail)
        return True

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        _pending_execution(),
        customer_id=501,
        trading_account_id=702,
        req=req,
    ))

    assert stage == "customer_paper_order_submitted"
    assert events == [{
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
        "order_role": "STRATEGY",
    }]


def test_replayed_pending_order_repairs_missing_audit_without_marking_new(monkeypatch):
    attempts = []

    async def audit_once(detail):
        attempts.append(detail)
        return False  # The idempotent audit row already exists.

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        _pending_execution(newly_submitted=False),
        customer_id=501,
        trading_account_id=702,
        req=req,
    ))

    assert stage == "customer_paper_order_pending"
    assert len(attempts) == 1
    assert attempts[0]["client_order_id"] == "cp-regression-order"


def test_recovery_wrapper_order_intent_is_audited_as_pending(monkeypatch):
    calls = []

    async def audit_once(detail):
        calls.append(detail)
        return False

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")
    recovered = {
        "status": "PROTECTIVE_EXIT_PENDING",
        "decision": "NO_TRADE",
        "execution": {
            "status": "WAITING_FOR_NEXT_COMPLETED_BAR",
            "order_intent": {
                "client_order_id": "cpx-recovered-exit",
                "order_id": 99002,
                "side": "SELL",
                "quantity": "0.25",
                "bar_timestamp": "2026-10-30T10:00:00+00:00",
                "order_role": "PROTECTIVE_EXIT",
                "newly_submitted": False,
            },
        },
    }

    stage = asyncio.run(main._customer_paper_submission_stage(
        recovered, customer_id=501, trading_account_id=702, req=req,
    ))

    assert stage == "customer_paper_order_pending"
    assert calls[0]["order_role"] == "PROTECTIVE_EXIT"
    assert calls[0]["client_order_id"] == "cpx-recovered-exit"


def test_non_pending_execution_does_not_change_its_result_stage(monkeypatch):
    calls = []

    async def audit_once(detail):
        calls.append(detail)
        return True

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        {"status": "APPENDED", "decision": "TRADE"},
        customer_id=501,
        trading_account_id=702,
        req=req,
    ))

    assert stage is None
    assert calls == []


def test_both_customer_paper_trade_paths_audit_first_submit_and_recovery():
    from pathlib import Path

    source = Path(main.__file__).read_text(encoding="utf-8")
    assert source.count("pending_stage = await _customer_paper_submission_stage(") == 4
    assert source.count("recover_customer_paper_spot_flow(") >= 2
    assert '"stage": pending_stage' in source
