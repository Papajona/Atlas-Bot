from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import main


def _request():
    return SimpleNamespace(symbol="BTC/USDT", exchange="binance", timeframe="1h")


def _terminal_execution():
    return {
        "status": "APPENDED",
        "decision": "TRADE",
        "order_intent": {
            "client_order_id": "paper-terminal-order-501",
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
        def all(self):
            return rows

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            statements.append(str(statement))
            return FakeResult()

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
