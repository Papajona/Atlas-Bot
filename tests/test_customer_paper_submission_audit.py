from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

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


def test_terminal_audit_failure_persists_retry_without_changing_trade_result(monkeypatch, caplog):
    persisted = []
    fallback = []

    async def no_prior_retries(_customer_id):
        return None

    async def fail_audit(_detail):
        raise RuntimeError("audit store unavailable")

    async def persist_retry(**kwargs):
        persisted.append(kwargs)

    async def safe_audit(event, detail, actor_id=None):
        fallback.append((event, detail, actor_id))

    monkeypatch.setattr(main, "_repair_customer_paper_submission_audit_retries", no_prior_retries)
    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", fail_audit)
    monkeypatch.setattr(main, "_persist_customer_paper_submission_audit_retry", persist_retry)
    monkeypatch.setattr(main, "_safe_audit", safe_audit)

    execution = _terminal_execution()
    result = asyncio.run(main._customer_paper_submission_stage(
        execution,
        customer_id=501,
        trading_account_id=702,
        req=_request(),
    ))

    assert result is None
    assert execution["decision"] == "TRADE"
    assert len(persisted) == 1
    assert persisted[0]["customer_id"] == 501
    assert persisted[0]["error_type"] == "RuntimeError"
    detail = persisted[0]["submission_detail"]
    assert detail["client_order_id"] == "paper-terminal-order-501"
    assert detail["execution_status"] == "APPENDED"
    assert fallback[0][0] == "CUSTOMER_PAPER_ORDER_SUBMISSION_AUDIT_INCOMPLETE"
    assert fallback[0][1]["submission_detail"] == detail
    assert fallback[0][2] == "customer:501"
    assert "paper-terminal-order-501" not in caplog.text
    assert "customer_id=501" not in caplog.text


def test_existing_primary_audit_retries_failed_external_delivery(monkeypatch):
    appended = []
    emitted = []
    emit_results = iter([False, True])

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def append_once(_db, **kwargs):
        appended.append(kwargs)
        created = len(appended) == 1
        return SimpleNamespace(event_hash="stable-event-hash"), created

    def emit(**kwargs):
        emitted.append(kwargs)
        return next(emit_results)

    monkeypatch.setattr(main, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(main, "append_audit_once", append_once)
    monkeypatch.setattr(main, "emit_security_audit", emit)

    detail = {
        "customer_id": 501,
        "client_order_id": "paper-external-retry-501",
        "side": "BUY",
    }

    with pytest.raises(RuntimeError, match="external customer paper audit delivery failed"):
        asyncio.run(main._audit_customer_paper_order_submitted_once(detail))

    created = asyncio.run(main._audit_customer_paper_order_submitted_once(
        detail, retry_external=True
    ))

    assert created is False
    assert len(appended) == 2
    assert all(item["idempotency_fields"] == ("customer_id", "client_order_id") for item in appended)
    assert len(emitted) == 2
    assert {item["event_hash"] for item in emitted} == {"stable-event-hash"}


def test_retry_repair_marks_success_only_after_external_delivery(monkeypatch):
    attempts = []
    state_changes = []
    payload = {
        "customer_id": 501,
        "client_order_id": "paper-retry-501",
        "order_id": 99001,
        "side": "SELL",
    }

    async def load(_customer_id):
        return [{
            "retry_id": 12,
            "customer_id": 501,
            "client_order_id": "paper-retry-501",
            "submission_detail": payload,
        }]

    async def audit(detail, *, retry_external=False):
        attempts.append((detail, retry_external))
        return False

    async def set_state(retry_id, **kwargs):
        state_changes.append((retry_id, kwargs))

    monkeypatch.setattr(main, "_load_customer_paper_submission_audit_retries", load)
    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", audit)
    monkeypatch.setattr(main, "_set_customer_paper_submission_audit_retry_state", set_state)

    asyncio.run(main._repair_customer_paper_submission_audit_retries(501))

    assert attempts == [(payload, True)]
    assert state_changes == [(12, {"status": "REPAIRED", "error_type": ""})]


def test_retry_repair_keeps_record_pending_after_external_delivery_failure(monkeypatch):
    state_changes = []

    async def load(_customer_id):
        return [{
            "retry_id": 13,
            "customer_id": 501,
            "client_order_id": "paper-retry-fail-501",
            "submission_detail": {
                "customer_id": 501,
                "client_order_id": "paper-retry-fail-501",
            },
        }]

    async def fail_audit(_detail, *, retry_external=False):
        assert retry_external is True
        raise RuntimeError("external sink unavailable")

    async def set_state(retry_id, **kwargs):
        state_changes.append((retry_id, kwargs))

    monkeypatch.setattr(main, "_load_customer_paper_submission_audit_retries", load)
    monkeypatch.setattr(main, "_audit_customer_paper_order_submitted_once", fail_audit)
    monkeypatch.setattr(main, "_set_customer_paper_submission_audit_retry_state", set_state)

    asyncio.run(main._repair_customer_paper_submission_audit_retries(501))

    assert state_changes == [(13, {
        "status": "PENDING",
        "error_type": "RuntimeError",
        "increment_attempts": True,
    })]


def test_retry_loader_quarantines_corrupt_record_and_keeps_valid_retry(monkeypatch, caplog):
    rows = [
        SimpleNamespace(id=1, client_order_id="good-order-501", raw_detail="good-ciphertext"),
        SimpleNamespace(id=2, client_order_id="corrupt-order-501", raw_detail="bad-ciphertext"),
    ]
    statements = []
    quarantined = []

    class FakeResult:
        rowcount = 1

        def all(self):
            return rows

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            sql = str(statement)
            statements.append(sql)
            if sql.lstrip().upper().startswith("SELECT"):
                return FakeResult()
            return SimpleNamespace(rowcount=1)

        async def commit(self):
            return None

    def decrypt(value):
        if value == "bad-ciphertext":
            raise RuntimeError("ciphertext authentication failed")
        return json.dumps({
            "customer_id": 501,
            "client_order_id": "good-order-501",
            "side": "BUY",
        })

    async def set_state(retry_id, **kwargs):
        quarantined.append((retry_id, kwargs))

    monkeypatch.setattr(main, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(main, "decrypt_text", decrypt)
    monkeypatch.setattr(main, "_set_customer_paper_submission_audit_retry_state", set_state)

    pending = asyncio.run(main._load_customer_paper_submission_audit_retries(501))

    assert pending == [{
        "retry_id": 1,
        "customer_id": 501,
        "client_order_id": "good-order-501",
        "submission_detail": {
            "customer_id": 501,
            "client_order_id": "good-order-501",
            "side": "BUY",
        },
    }]
    assert quarantined == [(2, {
        "status": "CORRUPT",
        "error_type": "RuntimeError",
        "increment_attempts": True,
    })]
    sql = statements[0].lower()
    assert "customer_paper_submission_audit_retries.customer_id" in sql
    assert "customer_paper_submission_audit_retries.status" in sql
    assert "limit" in sql
    assert "processing" in sql
    assert "updated_at" in sql
    claim_sql = [statement.lower() for statement in statements[1:]]
    assert len(claim_sql) == 2
    assert all(statement.startswith("update customer_paper_submission_audit_retries") for statement in claim_sql)
    assert all("status" in statement and "updated_at" in statement for statement in claim_sql)
    assert "corrupt-order-501" not in caplog.text
    assert "good-order-501" not in caplog.text


def test_retry_failure_logs_use_non_reversible_order_reference(caplog):
    try:
        raise RuntimeError("sensitive SQL details must not be logged")
    except RuntimeError as exc:
        main._log_customer_paper_submission_audit_retry_failure(
            event="customer_paper_submission_audit_retry_failed",
            customer_id=501,
            client_order_id="customer-visible-order-identity",
            retry_id=42,
            step="test",
            exc=exc,
        )

    assert "retry_id=42" in caplog.text
    assert "order_ref=" in caplog.text
    assert "customer-visible-order-identity" not in caplog.text
    assert "customer_id=501" not in caplog.text
    assert "sensitive SQL details" not in caplog.text



def test_retry_loader_skips_a_row_claimed_by_another_worker(monkeypatch):
    rows = [
        SimpleNamespace(id=21, client_order_id="raced-order-501", raw_detail="ciphertext"),
    ]
    calls = []

    class FakeResult:
        def all(self):
            return rows

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            sql = str(statement)
            calls.append(sql)
            if sql.lstrip().upper().startswith("SELECT"):
                return FakeResult()
            # A second worker won the compare-and-set claim first.
            return SimpleNamespace(rowcount=0)

    monkeypatch.setattr(main, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(
        main,
        "decrypt_text",
        lambda _value: json.dumps({
            "customer_id": 501,
            "client_order_id": "raced-order-501",
        }),
    )

    pending = asyncio.run(main._load_customer_paper_submission_audit_retries(501))

    assert pending == []
    assert len(calls) == 2
    assert calls[1].lstrip().upper().startswith("UPDATE ")
    assert "status" in calls[1].lower()


def test_retry_loader_uses_expiring_processing_lease(monkeypatch):
    rows = [
        SimpleNamespace(id=22, client_order_id="expired-lease-order-501", raw_detail="ciphertext"),
    ]
    statements = []

    class FakeResult:
        rowcount = 1

        def all(self):
            return rows

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            sql = str(statement)
            statements.append(sql)
            if sql.lstrip().upper().startswith("SELECT"):
                return FakeResult()
            return SimpleNamespace(rowcount=1)

        async def commit(self):
            return None

    monkeypatch.setattr(main, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(
        main,
        "decrypt_text",
        lambda _value: json.dumps({
            "customer_id": 501,
            "client_order_id": "expired-lease-order-501",
            "side": "BUY",
        }),
    )

    pending = asyncio.run(main._load_customer_paper_submission_audit_retries(501))

    assert len(pending) == 1
    assert pending[0]["retry_id"] == 22
    selection = statements[0].lower()
    assert "processing" in selection
    assert "updated_at" in selection
    assert "status" in selection
    assert statements[1].lstrip().upper().startswith("UPDATE ")


def test_audit_outage_does_not_turn_a_persisted_pending_order_into_http_error(monkeypatch, caplog):
    fallback = []
    persisted = []

    async def audit_once(_detail):
        raise RuntimeError("audit store unavailable")

    async def persist_retry(**kwargs):
        persisted.append(kwargs)

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
    assert len(persisted) == 1
    assert persisted[0]["customer_id"] == 501
    assert persisted[0]["submission_detail"]["client_order_id"] == "cp-regression-order"
    failure_logs = [
        record for record in caplog.records
        if record.getMessage().startswith("customer_paper_submission_audit_failed ")
    ]
    assert len(failure_logs) == 1
    assert "order_ref=" in failure_logs[0].getMessage()
    assert "cp-regression-order" not in failure_logs[0].getMessage()
    assert "audit store unavailable" not in failure_logs[0].getMessage()
    assert fallback[0][0] == "CUSTOMER_PAPER_ORDER_SUBMISSION_AUDIT_INCOMPLETE"
    assert fallback[0][1]["submission_detail"]["client_order_id"] == "cp-regression-order"


@pytest.fixture(autouse=True)
def _disable_retry_repair_for_submission_stage_unit_tests(monkeypatch):
    async def no_retries(_customer_id):
        return None
    monkeypatch.setattr(main, "_repair_customer_paper_submission_audit_retries", no_retries)


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
        return SimpleNamespace(event_hash="hash-1"), True

    monkeypatch.setattr(main, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(main, "append_audit_once", append_once)
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
    assert len(events) == 1
    assert events[0]["client_order_id"] == "legacy-ownerless-buy"
    assert events[0]["order_id"] == "legacy-order-7"
    assert events[0]["status"] == "SUBMITTED"
    assert events[0]["execution_status"] == "EXPIRED"

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

