from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import main


def _pending_execution(*, newly_submitted: bool = True, order_role: str = "STRATEGY") -> dict:
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
            "submission_status": "WAITING_FOR_NEXT_COMPLETED_BAR",
            "execution_status": "WAITING_FOR_NEXT_COMPLETED_BAR",
            "order_role": order_role,
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
        "execution_status": "WAITING_FOR_NEXT_COMPLETED_BAR",
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



def test_audit_outage_does_not_turn_a_persisted_pending_order_into_http_error(monkeypatch):
    fallback = []

    async def audit_once(_detail):
        raise RuntimeError("audit store unavailable")

    async def safe_audit(event, detail):
        fallback.append((event, detail))

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    monkeypatch.setattr(main, "_safe_audit", safe_audit)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        _pending_execution(),
        customer_id=501,
        trading_account_id=702,
        req=req,
    ))

    assert stage == "customer_paper_order_audit_incomplete"
    assert fallback == [(
        "CUSTOMER_PAPER_ORDER_SUBMISSION_AUDIT_INCOMPLETE",
        {
            "customer_id": 501,
            "trading_account_id": 702,
            "client_order_id": "cp-regression-order",
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "submission_status": "WAITING_FOR_NEXT_COMPLETED_BAR",
            "execution_status": "WAITING_FOR_NEXT_COMPLETED_BAR",
            "reason": "RuntimeError",
        },
    )]


def test_protective_exit_role_survives_the_submission_audit_stage(monkeypatch):
    audits = []

    async def audit_once(detail):
        audits.append(detail)
        return True

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        _pending_execution(newly_submitted=True, order_role="PROTECTIVE_EXIT"),
        customer_id=501,
        trading_account_id=702,
        req=req,
    ))

    assert stage == "customer_paper_order_submitted"
    assert audits[0]["order_role"] == "PROTECTIVE_EXIT"



def test_terminal_fill_repairs_submission_audit_without_being_mislabeled_pending(monkeypatch):
    events = []

    async def audit_once(detail):
        events.append(detail)
        return True

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")
    execution = {
        "status": "APPENDED",
        "decision": "TRADE",
        "order_intent": {
            "client_order_id": "cp-terminal-order",
            "order_id": 44100,
            "side": "BUY",
            "quantity": "0.25",
            "bar_timestamp": "2026-10-30T10:00:00+00:00",
            "submission_status": "WAITING_FOR_FILL_LATENCY",
            "execution_status": "APPENDED",
            "order_role": "STRATEGY",
            "newly_submitted": False,
        },
    }

    stage = asyncio.run(main._customer_paper_submission_stage(
        execution, customer_id=501, trading_account_id=702, req=req,
    ))

    assert stage is None
    assert len(events) == 1
    assert events[0]["status"] == "WAITING_FOR_FILL_LATENCY"
    assert events[0]["execution_status"] == "APPENDED"


def test_terminal_fill_audit_failure_does_not_replace_filled_result_with_no_trade(monkeypatch):
    fallback = []

    async def audit_once(_detail):
        raise RuntimeError("audit database unavailable")

    async def safe_audit(event, detail):
        fallback.append((event, detail))

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    monkeypatch.setattr(main, "_safe_audit", safe_audit)
    execution = {
        "status": "APPENDED",
        "decision": "TRADE",
        "order_intent": {
            "client_order_id": "cp-terminal-audit-failure",
            "order_id": 44990,
            "side": "BUY",
            "quantity": "0.25",
            "bar_timestamp": "2026-10-30T10:00:00+00:00",
            "submission_status": "WAITING_FOR_FILL_LATENCY",
            "execution_status": "APPENDED",
            "order_role": "STRATEGY",
            "newly_submitted": False,
        },
    }
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        execution, customer_id=501, trading_account_id=702, req=req,
    ))

    assert stage is None
    assert execution["decision"] == "TRADE"
    assert fallback[0][0] == "CUSTOMER_PAPER_ORDER_SUBMISSION_AUDIT_INCOMPLETE"
    assert fallback[0][1]["execution_status"] == "APPENDED"


def test_customer_submission_audit_uses_customer_actor_even_with_system_context(monkeypatch):
    captured = {}
    class DummySession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False

    async def append_once(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(event_hash="hash-1"), True

    monkeypatch.setattr(main, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(main, "append_audit_once", append_once)
    monkeypatch.setattr(main, "emit_security_audit", lambda **_kwargs: None)

    created = asyncio.run(main._audit_customer_paper_order_submitted_once({
        "customer_id": 501,
        "client_order_id": "cp-actor-order",
    }))

    assert created is True
    assert captured["actor_id"] == "customer:501"


def test_recovered_terminal_orders_keep_submission_intent_for_audit_repair():
    import inspect
    from app.customer_paper_flow import process_customer_paper_orders

    source = inspect.getsource(process_customer_paper_orders)
    assert 'recovered["order_intent"] = {' in source
    assert 'if str(recovered.get("status") or "").upper() in {' not in source
    assert '"submission_status": str(order.get("status") or "SUBMITTED").upper()' in source


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
