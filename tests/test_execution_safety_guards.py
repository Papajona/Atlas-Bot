import asyncio

import pytest

from app.config import settings
from app.main import ExecuteRequest, _require_signal_timestamp_for_execution, _run_ai_safety_review


def _request(**overrides):
    values = {
        "asset": "crypto",
        "symbol": "BTC/USDT:USDT",
        "exchange": "bybit",
        "timeframe": "1h",
        "side": "buy",
        "quantity": 1,
        "force_paper": True,
        "demo_forex": False,
    }
    values.update(overrides)
    return ExecuteRequest(**values)


def test_paper_execution_may_omit_signal_timestamp(monkeypatch):
    monkeypatch.setattr(settings, "require_signal_timestamp_for_live", True)
    _require_signal_timestamp_for_execution(_request(force_paper=True))


def test_live_execution_requires_signal_timestamp(monkeypatch):
    monkeypatch.setattr(settings, "require_signal_timestamp_for_live", True)
    with pytest.raises(Exception, match="signal_timestamp is required for live or broker-demo execution"):
        _require_signal_timestamp_for_execution(_request(force_paper=False))


def test_broker_demo_requires_signal_timestamp(monkeypatch):
    monkeypatch.setattr(settings, "require_signal_timestamp_for_live", True)
    with pytest.raises(Exception, match="signal_timestamp is required for broker-demo execution"):
        _require_signal_timestamp_for_execution(_request(demo_forex=True))


def test_timestamp_requirement_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "require_signal_timestamp_for_live", False)
    _require_signal_timestamp_for_execution(_request(force_paper=False))


def test_present_timestamp_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "require_signal_timestamp_for_live", True)
    _require_signal_timestamp_for_execution(
        _request(force_paper=False, signal_timestamp="2026-10-07T07:00:00+00:00")
    )


@pytest.mark.asyncio
async def test_ai_safety_review_timeout_is_enforced(monkeypatch):
    async def slow_review(_packet):
        await asyncio.sleep(0.05)
        return {"safe": True}

    monkeypatch.setattr("app.main.dual_ai_trade_safety_review", slow_review)
    monkeypatch.setattr(settings, "ai_strategy_provider_timeout_seconds", 0.001)

    with pytest.raises(asyncio.TimeoutError):
        await _run_ai_safety_review({"symbol": "BTC/USDT:USDT"})


@pytest.mark.asyncio
async def test_ai_safety_review_returns_provider_result_before_timeout(monkeypatch):
    async def fast_review(_packet):
        return {"safe": True, "configured": True}

    monkeypatch.setattr("app.main.dual_ai_trade_safety_review", fast_review)
    monkeypatch.setattr(settings, "ai_strategy_provider_timeout_seconds", 1.0)

    assert await _run_ai_safety_review({"symbol": "BTC/USDT:USDT"}) == {
        "safe": True,
        "configured": True,
    }
