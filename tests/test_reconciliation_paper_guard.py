"""The exchange reconciliation loop must never touch exchange state in paper mode.

``sync_live_account`` overwrites AppState equity/positions with the exchange account. Every other
live gate (execution.py, live_execution.py, readiness) requires ``not paper_trading``; the
reconciliation loop must too, so a stray LIVE_TRADING_ENABLED=true cannot replace paper equity.
"""
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


def _run_one_cycle(monkeypatch, *, paper: bool):
    calls = []
    monkeypatch.setattr(main_module.settings, "live_trading_enabled", True)
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

    async def record(name, *_a, **_k):
        calls.append(name)

    async def reconcile(*_a, **_k):
        calls.append("reconcile")

    async def sync_live_account(*_a, **_k):
        calls.append("sync_live_account")

    async def loss_limits(*_a, **_k):
        calls.append("loss_limits")

    async def noop(*_a, **_k):
        return 0

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main_module, "reconcile", reconcile)
    monkeypatch.setattr(main_module, "sync_live_account", sync_live_account)
    monkeypatch.setattr(main_module, "enforce_platform_loss_limits", loss_limits)
    monkeypatch.setattr(main_module, "reconcile_customer_live_orders", noop)
    monkeypatch.setattr(main_module, "_record_reconciliation_success", noop)
    monkeypatch.setattr(main_module, "_record_reconciliation_failure", noop)
    monkeypatch.setattr(main_module, "_mark_worker_loop_success", lambda *_a, **_k: None)
    monkeypatch.setattr(main_module, "_mark_worker_loop_failure", lambda *_a, **_k: None)
    monkeypatch.setattr(main_module, "SessionLocal", lambda: _FakeSession())

    with pytest.raises(_StopLoop):
        asyncio.run(main_module._reconciliation_loop())
    return calls


def test_paper_mode_never_syncs_exchange_account_even_if_live_flag_and_keys_are_set(monkeypatch):
    calls = _run_one_cycle(monkeypatch, paper=True)
    assert "sync_live_account" not in calls
    assert "reconcile" not in calls
    assert "loss_limits" not in calls


def test_live_mode_still_reconciles_and_syncs_exchange_account(monkeypatch):
    calls = _run_one_cycle(monkeypatch, paper=False)
    assert "reconcile" in calls and "sync_live_account" in calls
