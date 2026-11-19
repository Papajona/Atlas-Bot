"""Paper-mode reconciliation must resolve existing platform live orders safely."""
import asyncio

import pytest

from app import main as main_module


class _StopLoop(Exception):
    pass


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_args, **_kwargs):
        class _Result:
            def scalars(self):
                return self

            def all(self):
                return []

        return _Result()


def _run_one_cycle(monkeypatch, *, paper: bool, reconciliation_account: dict):
    calls = []
    failures = []
    successes = []
    monkeypatch.setattr(main_module.settings, "live_trading_enabled", not paper)
    monkeypatch.setattr(main_module.settings, "paper_trading", paper)
    monkeypatch.setattr(main_module.settings, "exchange_api_key", "k")
    monkeypatch.setattr(main_module.settings, "exchange_api_secret", "s")
    monkeypatch.setattr(main_module.settings, "oanda_account_id", "")
    monkeypatch.setattr(main_module.settings, "oanda_api_token", "")

    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise _StopLoop()

    async def reconcile(exchange, symbol):
        calls.append(("reconcile", exchange, symbol))
        return {
            "results": [{"trade_id": 42, "status": "FILLED", "resolved": True}],
            "account": reconciliation_account,
        }

    async def enforce_loss_limits():
        calls.append(("loss_limits",))

    async def noop(*_args, **_kwargs):
        return 0

    async def record_success(scope):
        successes.append(scope)

    async def record_failure(scope, exc):
        failures.append((scope, str(exc)))
        return 1

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main_module, "reconcile", reconcile)
    monkeypatch.setattr(main_module, "enforce_platform_loss_limits", enforce_loss_limits)
    monkeypatch.setattr(main_module, "reconcile_customer_live_orders", noop)
    monkeypatch.setattr(main_module, "_record_reconciliation_success", record_success)
    monkeypatch.setattr(main_module, "_record_reconciliation_failure", record_failure)
    monkeypatch.setattr(main_module, "_mark_worker_loop_success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "_mark_worker_loop_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "SessionLocal", lambda: _FakeSession())

    with pytest.raises(_StopLoop):
        asyncio.run(main_module._reconciliation_loop())
    return calls, failures, successes


def test_paper_switch_still_reconciles_an_outstanding_platform_order_without_syncing_live_equity(monkeypatch):
    calls, failures, successes = _run_one_cycle(
        monkeypatch,
        paper=True,
        reconciliation_account={"paper": True},
    )

    assert ("reconcile", main_module.settings.default_exchange, main_module.settings.default_symbol) in calls
    assert ("loss_limits",) not in calls
    assert "platform-loss-limits" in successes
    assert not failures


def test_live_mode_enforces_loss_limits_only_after_reconciliation_returns_fresh_live_equity(monkeypatch):
    calls, failures, successes = _run_one_cycle(
        monkeypatch,
        paper=False,
        reconciliation_account={"equity": 12_500.0, "positions_synced": 0},
    )

    assert calls[0][0] == "reconcile"
    assert ("loss_limits",) in calls
    assert "crypto" in successes
    assert "platform-loss-limits" in successes
    assert not failures


def test_live_mode_records_broker_account_sync_error_in_crypto_scope(monkeypatch):
    calls, failures, successes = _run_one_cycle(
        monkeypatch,
        paper=False,
        reconciliation_account={"error": "broker balance unavailable"},
    )

    assert ("reconcile", main_module.settings.default_exchange, main_module.settings.default_symbol) in calls
    assert ("loss_limits",) not in calls
    assert any(
        scope == "crypto" and "account synchronization failed" in message
        for scope, message in failures
    )
    assert "crypto" not in successes
    assert "platform-loss-limits" not in successes


def test_live_mode_records_missing_live_equity_as_loss_limit_failure(monkeypatch):
    calls, failures, successes = _run_one_cycle(
        monkeypatch,
        paper=False,
        reconciliation_account={"balance": 12_500.0},
    )

    assert ("reconcile", main_module.settings.default_exchange, main_module.settings.default_symbol) in calls
    assert ("loss_limits",) not in calls
    assert any(
        scope == "platform-loss-limits" and "fresh live-equity snapshot" in message
        for scope, message in failures
    )
    assert "crypto" in successes
    assert "platform-loss-limits" not in successes
