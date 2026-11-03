from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import main


def _successful_external_delivery(monkeypatch):
    async def claim(_event_hash):
        return "test-claim-token"

    async def finish(_event_hash, *, claim_token, **_kwargs):
        assert claim_token == "test-claim-token"
        return True

    monkeypatch.setattr(main, "claim_external_audit_delivery", claim)
    monkeypatch.setattr(main, "finish_external_audit_delivery", finish)



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



def test_audit_outage_does_not_turn_a_persisted_pending_order_into_http_error(monkeypatch, caplog):
    fallback = []
    persisted_retries = []

    async def audit_once(_detail):
        raise RuntimeError("audit store unavailable")

    async def persist_retry(**kwargs):
        persisted_retries.append(kwargs)

    async def safe_audit(event, detail, **_kwargs):
        fallback.append((event, detail))

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    monkeypatch.setattr(main, "_persist_customer_paper_submission_audit_retry", persist_retry)
    monkeypatch.setattr(main, "_safe_audit", safe_audit)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        _pending_execution(),
        customer_id=501,
        trading_account_id=702,
        req=req,
    ))

    assert stage == "customer_paper_order_audit_incomplete"
    failure_logs = [
        record for record in caplog.records
        if record.getMessage().startswith("customer_paper_submission_audit_failed ")
    ]
    assert len(failure_logs) == 1
    failure_message = failure_logs[0].getMessage()
    assert "customer_id=501" in failure_message
    assert "client_order_id=cp-regression-order" in failure_message
    assert "error_type=RuntimeError" in failure_message
    assert "traceback_frames=" in failure_message
    assert "audit store unavailable" not in failure_message
    assert failure_logs[0].exc_info is None
    assert len(persisted_retries) == 1
    assert persisted_retries[0]["customer_id"] == 501
    assert persisted_retries[0]["submission_detail"]["client_order_id"] == "cp-regression-order"
    assert persisted_retries[0]["error_type"] == "RuntimeError"
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

    async def safe_audit(event, detail, **_kwargs):
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
        async def commit(self):
            return None

    async def append_once(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(event_hash="a" * 64), True

    async def claim_delivery(_event_hash):
        return "test-claim-token"

    async def finish_delivery(_event_hash, **_kwargs):
        return True

    monkeypatch.setattr(main, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(main, "append_audit_once", append_once)
    monkeypatch.setattr(main, "claim_external_audit_delivery", claim_delivery)
    monkeypatch.setattr(main, "finish_external_audit_delivery", finish_delivery)
    monkeypatch.setattr(main, "emit_security_audit", lambda **_kwargs: True)

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


def test_ownerless_legacy_expiry_repairs_submission_audit_without_reopening_order(monkeypatch):
    events = []

    async def audit_once(detail):
        events.append(detail)
        return True

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")
    execution = {
        "status": "EXPIRED_LEGACY_OWNERLESS",
        "decision": "NO_TRADE",
        "fill": {"status": "EXPIRED"},
        "order_intent": {
            "client_order_id": "legacy-ownerless-buy",
            "order_id": "legacy-order-7",
            "side": "BUY",
            "quantity": "0.25",
            "bar_timestamp": "2026-10-30T10:00:00+00:00",
            "submission_status": "SUBMITTED",
            "execution_status": "EXPIRED",
            "order_role": "LEGACY_MANUAL_RECONCILIATION",
            "newly_submitted": False,
        },
    }

    stage = asyncio.run(main._customer_paper_submission_stage(
        execution, customer_id=501, trading_account_id=702, req=req,
    ))

    assert stage is None
    legacy_events = [
        detail for detail in events
        if detail.get("client_order_id") == "legacy-ownerless-buy"
    ]
    assert len(legacy_events) == 1
    assert legacy_events[0]["order_id"] == "legacy-order-7"
    assert legacy_events[0]["status"] == "SUBMITTED"
    assert legacy_events[0]["execution_status"] == "EXPIRED"


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


def test_submission_stage_handles_missing_intent_and_missing_status_without_crashing():
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")

    stage = asyncio.run(main._customer_paper_submission_stage(
        {"decision": "NO_TRADE", "execution": {}},
        customer_id=501,
        trading_account_id=702,
        req=req,
    ))

    assert stage is None


def test_terminal_replay_audit_prefers_persisted_filled_status_over_replayed_wrapper(monkeypatch):
    events = []

    async def audit_once(detail):
        events.append(detail)
        return True

    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit_once)
    req = SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")
    execution = {
        "status": "REPLAYED",
        "decision": "TRADE",
        "execution": {"status": "REPLAYED"},
        "order_intent": {
            "client_order_id": "cp-terminal-replayed-order",
            "order_id": 44101,
            "side": "BUY",
            "quantity": "0.25",
            "bar_timestamp": "2026-10-30T10:00:00+00:00",
            "submission_status": "SUBMITTED",
            "execution_status": "FILLED",
            "order_role": "STRATEGY",
            "newly_submitted": False,
        },
    }

    stage = asyncio.run(main._customer_paper_submission_stage(
        execution, customer_id=501, trading_account_id=702, req=req,
    ))

    assert stage is None
    assert len(events) == 1
    assert events[0]["status"] == "SUBMITTED"
    assert events[0]["execution_status"] == "FILLED"


def test_external_submission_audit_is_retried_when_database_event_already_exists(monkeypatch):
    captured = {}
    emitted = []

    class DummySession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one=lambda: json.dumps(original_payload))

    original_payload = {
        "customer_id": 501,
        "client_order_id": "cp-replayed-external-audit",
        "status": "WAITING_FOR_NEXT_COMPLETED_BAR",
        "execution_status": "WAITING_FOR_NEXT_COMPLETED_BAR",
    }
    claim_results = iter(["claim-1", "claim-2", None])
    create_results = iter([True, False, False])
    emit_results = iter([False, True])
    delivery_results = []

    async def claim(_event_hash):
        return next(claim_results)

    async def finish(_event_hash, *, claim_token, delivered, error_type=None):
        delivery_results.append((delivered, error_type, claim_token))
        return True

    async def append_with_replay(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id=123,
            event_hash="stable-event-hash",
            detail=json.dumps(original_payload),
            actor_id="customer:501",
        ), next(create_results)

    def emit(**kwargs):
        emitted.append(kwargs)
        return next(emit_results)

    monkeypatch.setattr(main, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(main, "append_audit_once", append_with_replay)
    monkeypatch.setattr(main, "claim_external_audit_delivery", claim)
    monkeypatch.setattr(main, "finish_external_audit_delivery", finish)
    monkeypatch.setattr(main, "emit_security_audit", emit)

    with pytest.raises(RuntimeError, match="did not confirm delivery"):
        asyncio.run(main._audit_customer_paper_order_submitted_once({
            "customer_id": 501,
            "client_order_id": "cp-replayed-external-audit",
        }))
    created = asyncio.run(main._audit_customer_paper_order_submitted_once({
        "customer_id": 501,
        "client_order_id": "cp-replayed-external-audit",
    }))
    replayed = asyncio.run(main._audit_customer_paper_order_submitted_once({
        "customer_id": 501,
        "client_order_id": "cp-replayed-external-audit",
    }))

    assert created is False and replayed is False
    assert captured["idempotency_fields"] == ("customer_id", "client_order_id")
    assert len(emitted) == 2  # One failed attempt and one successful retry, not every replay.
    assert emitted[0]["event_hash"] == emitted[1]["event_hash"] == "stable-event-hash"
    assert emitted[0]["actor_id"] == emitted[1]["actor_id"] == "customer:501"
    assert emitted[0]["detail"] == emitted[1]["detail"] == original_payload
    assert delivery_results == [
        (False, "DeliveryNotConfirmed", "claim-1"),
        (True, None, "claim-2"),
    ]


def test_external_submission_audit_rejects_unconfirmed_second_copy(monkeypatch):
    class DummySession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one=lambda: json.dumps({
                "customer_id": 501,
                "client_order_id": "cp-failed-external-audit",
            }))

    async def append_once(_db, **kwargs):
        return SimpleNamespace(
            id=124,
            event_hash="stable-event-hash",
            actor_id=kwargs["actor_id"],
        ), False

    monkeypatch.setattr(main, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(main, "append_audit_once", append_once)
    monkeypatch.setattr(main, "emit_security_audit", lambda **_kwargs: False)
    _successful_external_delivery(monkeypatch)

    with pytest.raises(RuntimeError, match="did not confirm delivery"):
        asyncio.run(main._audit_customer_paper_order_submitted_once({
            "customer_id": 501,
            "client_order_id": "cp-failed-external-audit",
        }))



def test_damaged_encrypted_audit_payload_does_not_block_existing_order_replay(monkeypatch, caplog):
    class DummySession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def execute(self, _statement):
            raise RuntimeError("encrypted audit detail authentication failed")

    finished = []
    async def append_once(_db, **_kwargs):
        return SimpleNamespace(
            id=125,
            event_hash="damaged-event-hash",
            actor_id="customer:501",
        ), False

    async def claim(_event_hash):
        return "claim-damaged"

    async def finish(_event_hash, *, claim_token, delivered, error_type=None):
        finished.append((claim_token, delivered, error_type))
        return True

    monkeypatch.setattr(main, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(main, "append_audit_once", append_once)
    monkeypatch.setattr(main, "claim_external_audit_delivery", claim)
    monkeypatch.setattr(main, "finish_external_audit_delivery", finish)
    monkeypatch.setattr(
        main, "emit_security_audit",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("damaged payload must not reach sink")),
    )

    created = asyncio.run(main._audit_customer_paper_order_submitted_once({
        "customer_id": 501,
        "client_order_id": "cp-damaged-encrypted-audit",
    }))

    assert created is False
    assert finished == [("claim-damaged", False, "RuntimeError")]
    assert any(
        record.getMessage() == "customer paper audit payload could not be loaded for external delivery"
        for record in caplog.records
    )

def test_submission_audit_distinguishes_active_external_delivery_lease(monkeypatch):
    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def append_once(_db, **_kwargs):
        return SimpleNamespace(
            id=126,
            event_hash="active-lease-event-hash",
            actor_id="customer:501",
        ), False

    async def claim(_event_hash):
        return main.EXTERNAL_AUDIT_DELIVERY_IN_FLIGHT

    monkeypatch.setattr(main, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(main, "append_audit_once", append_once)
    monkeypatch.setattr(main, "claim_external_audit_delivery", claim)
    monkeypatch.setattr(
        main,
        "emit_security_audit",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a competing in-flight delivery must not send twice")
        ),
    )

    with pytest.raises(main.CustomerPaperSubmissionAuditDeliveryInFlight):
        asyncio.run(main._audit_customer_paper_order_submitted_once({
            "customer_id": 501,
            "client_order_id": "cp-active-lease-order",
        }))
