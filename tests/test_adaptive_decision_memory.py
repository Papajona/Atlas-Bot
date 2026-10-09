from datetime import datetime, timezone
from types import SimpleNamespace

from app import main


def _request():
    return SimpleNamespace(
        asset="crypto",
        symbol="BTC/USDT",
        exchange="binance",
        timeframe="1h",
    )


def test_no_trade_memory_records_gate_and_point_in_time_snapshot_without_future_outcomes():
    started = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    result = {
        "decision": "NO_TRADE",
        "stage": "adaptive_model_confirmation",
        "analysis": {
            "decision": "TRADE",
            "timestamp": "2026-10-09T09:00:00+00:00",
            "trade_plan": {"side": "buy", "entry_price": 100.0, "stop_loss_price": 98.0},
            "data_quality": {"safe": True, "reasons": []},
        },
        "ml_signal": {"signal": -0.4, "model_version": "sha256:abc"},
        "ai_review": {"safe": True, "configured": True},
        "strategy_router": {
            "status": "OK", "strategy": "trend", "regime": "TREND_UP",
            "validation_gate": {"passed": True},
        },
        "execution": {},
        "realized_pnl": 999.0,
        "exit_price": 123.0,
    }

    record = main._build_adaptive_decision_memory(
        decision_id="decision-1",
        bot_id=12,
        customer_id=34,
        mode="PAPER",
        req=_request(),
        result=result,
        cycle_started_at=started,
    )

    assert record["decision"] == "NO_TRADE"
    assert record["stage"] == "adaptive_model_confirmation"
    assert record["market_timestamp"] == "2026-10-09T09:00:00+00:00"
    assert record["market_timestamp_source"] == "analysis_timestamp"
    assert record["customer_id"] == 34
    assert record["mode"] == "PAPER"
    assert record["decision_context"]["ml_signal"]["signal"] == -0.4
    assert record["decision_context"]["strategy_router"]["strategy"] == "trend"
    assert len(record["snapshot_sha256"]) == 64
    assert "realized_pnl" not in record
    assert "exit_price" not in record
    assert "realized_pnl" not in record["decision_context"]
    assert "exit_price" not in record["decision_context"]


def test_decision_memory_redacts_secret_fields_from_nested_evidence():
    started = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    result = {
        "decision": "NO_TRADE",
        "stage": "deterministic_gate",
        "analysis": {
            "timestamp": "2026-10-09T09:00:00+00:00",
            "rules": [{"rule": "spread", "api_token": "must-not-persist", "passed": False}],
        },
    }
    record = main._build_adaptive_decision_memory(
        decision_id="decision-2",
        bot_id=12,
        customer_id=34,
        mode="PAPER",
        req=_request(),
        result=result,
        cycle_started_at=started,
    )
    assert "api_token" not in str(record)
    assert "must-not-persist" not in str(record)


def test_decision_memory_audit_failure_does_not_raise(monkeypatch):
    calls = []

    async def fail_audit(payload):
        calls.append(payload)
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(main, "_append_adaptive_decision_audit", fail_audit)

    async def run():
        await main._record_adaptive_decision_memory(
            decision_id="decision-3",
            bot_id=12,
            customer_id=34,
            mode="PAPER",
            req=_request(),
            result={"decision": "NO_TRADE", "stage": "test_gate"},
            cycle_started_at=datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc),
        )

    import asyncio
    asyncio.run(run())
    assert calls
    assert calls[0]["record_type"] == "adaptive_trade_decision"
    assert calls[0]["decision"] == "NO_TRADE"


def test_trade_memory_keeps_execution_ack_separate_from_decision_snapshot():
    record = main._build_adaptive_decision_memory(
        decision_id="decision-trade",
        bot_id=None,
        customer_id=34,
        mode="PAPER",
        req=_request(),
        result={
            "decision": "TRADE",
            "stage": "execution",
            "mode": "PAPER",
            "analysis": {"timestamp": "2026-10-09T09:00:00+00:00", "decision": "TRADE"},
            "execution": {"trade_id": 123, "status": "SUBMITTED", "mode": "PAPER"},
            "realized_pnl": 5000.0,
        },
        cycle_started_at=datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc),
    )
    assert record["bot_id"] is None
    assert record["execution_ack"] == {"trade_id": 123, "status": "SUBMITTED", "mode": "PAPER"}
    assert "realized_pnl" not in str(record)
    assert record["effective_mode"] == "PAPER"



def test_external_audit_summary_excludes_full_customer_and_model_snapshot():
    summary = main._adaptive_decision_external_summary({
        "decision_id": "decision-4",
        "bot_id": 12,
        "customer_id": 34,
        "asset": "crypto",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "decision": "NO_TRADE",
        "stage": "ai_safety_gate",
        "market_timestamp": "2026-10-09T09:00:00+00:00",
        "snapshot_sha256": "a" * 64,
        "decision_context": {"private": "must-stay-in-encrypted-db"},
    })
    assert summary["decision_id"] == "decision-4"
    assert summary["snapshot_sha256"] == "a" * 64
    assert "customer_id" not in summary
    assert "decision_context" not in summary
    assert "private" not in str(summary)


def test_snapshot_hash_changes_when_decision_identity_changes():
    kwargs = {
        "bot_id": 12,
        "customer_id": 34,
        "mode": "PAPER",
        "req": _request(),
        "result": {
            "decision": "NO_TRADE",
            "stage": "risk_gate",
            "analysis": {"timestamp": "2026-10-09T09:00:00+00:00"},
        },
        "cycle_started_at": datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc),
    }
    first = main._build_adaptive_decision_memory(decision_id="decision-a", **kwargs)
    second = main._build_adaptive_decision_memory(decision_id="decision-b", **kwargs)
    assert first["snapshot_sha256"] != second["snapshot_sha256"]


def test_decision_memory_keeps_bounded_gate_reason_and_error_code():
    record = main._build_adaptive_decision_memory(
        decision_id="decision-reason",
        bot_id=12,
        customer_id=34,
        mode="PAPER",
        req=_request(),
        result={
            "decision": "NO_TRADE",
            "stage": "runtime_gate",
            "reason": "platform_kill_switch",
            "error_type": "RiskBlocked",
            "http_status": 409,
        },
        cycle_started_at=datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc),
    )
    assert record["decision_context"]["decision_outcome"] == {
        "reason": "platform_kill_switch",
        "error_type": "RiskBlocked",
        "http_status": 409,
    }


def test_trade_decision_memory_preserves_selected_strategy_and_regime():
    record = main._build_adaptive_decision_memory(
        decision_id="decision-strategy",
        bot_id=12,
        customer_id=34,
        mode="PAPER",
        req=_request(),
        result={
            "decision": "TRADE",
            "stage": "execution",
            "strategy": "atlas-adaptive-autonomous-v1",
            "regime": "TREND_UP",
            "strategy_router": {"status": "OK", "strategy": "trend", "regime": "TREND_UP"},
            "analysis": {"timestamp": "2026-10-09T09:00:00+00:00"},
        },
        cycle_started_at=datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc),
    )
    assert record["decision_context"]["execution_policy"] == {
        "strategy": "atlas-adaptive-autonomous-v1",
        "regime": "TREND_UP",
    }
    assert record["decision_context"]["strategy_router"]["strategy"] == "trend"
