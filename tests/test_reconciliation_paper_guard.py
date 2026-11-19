"""Paper-mode reconciliation must resolve existing platform live orders safely."""
import asyncio
from types import SimpleNamespace

import pytest

from app import execution as execution_module
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


def _run_one_cycle(
    monkeypatch, *, paper: bool, reconciliation_account: dict, live_enabled: bool | None = None
):
    calls = []
    failures = []
    successes = []
    monkeypatch.setattr(
        main_module.settings,
        "live_trading_enabled",
        not paper if live_enabled is None else live_enabled,
    )
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

    async def reconcile(exchange, symbol=None):
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

    assert ("reconcile", main_module.settings.default_exchange, None) in calls
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

    assert ("reconcile", main_module.settings.default_exchange, None) in calls
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

    assert ("reconcile", main_module.settings.default_exchange, None) in calls
    assert ("loss_limits",) not in calls
    assert any(
        scope == "platform-loss-limits" and "fresh live-equity snapshot" in message
        for scope, message in failures
    )
    assert "crypto" in successes
    assert "platform-loss-limits" not in successes


def test_live_trading_disabled_skips_live_equity_loss_limits_even_when_not_paper(monkeypatch):
    calls, failures, successes = _run_one_cycle(
        monkeypatch,
        paper=False,
        live_enabled=False,
        reconciliation_account={"paper": True},
    )

    assert ("reconcile", main_module.settings.default_exchange, None) in calls
    assert ("loss_limits",) not in calls
    assert "platform-loss-limits" in successes
    assert not failures


def test_missing_credentials_probe_failure_is_scoped_and_other_scopes_continue(monkeypatch):
    calls = []
    failures = []
    successes = []
    session_executes = {"count": 0}
    sleeps = {"count": 0}

    monkeypatch.setattr(main_module.settings, "exchange_api_key", "")
    monkeypatch.setattr(main_module.settings, "exchange_api_secret", "")
    monkeypatch.setattr(main_module.settings, "oanda_account_id", "practice-account")
    monkeypatch.setattr(main_module.settings, "oanda_api_token", "practice-token")
    monkeypatch.setattr(main_module.settings, "oanda_practice", True)
    monkeypatch.setattr(main_module.settings, "forex_demo_enabled", True)
    monkeypatch.setattr(main_module.settings, "paper_trading", True)
    monkeypatch.setattr(main_module.settings, "live_trading_enabled", False)

    class _ProbeFailureSession(_FakeSession):
        async def execute(self, *_args, **_kwargs):
            session_executes["count"] += 1
            if session_executes["count"] == 1:
                raise RuntimeError("database unavailable during unresolved-order probe")

            class _Result:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return _Result()

    async def fake_sleep(_seconds):
        sleeps["count"] += 1
        if sleeps["count"] > 1:
            raise _StopLoop()

    async def reconcile(exchange, symbol=None):
        calls.append(("reconcile", exchange, symbol))
        return {"account": {"paper": True}}

    async def noop(*_args, **_kwargs):
        return 0

    async def record_success(scope):
        successes.append(scope)

    async def record_failure(scope, exc):
        failures.append((scope, str(exc)))
        return 1

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main_module, "reconcile", reconcile)
    monkeypatch.setattr(main_module, "reconcile_customer_live_orders", noop)
    monkeypatch.setattr(main_module, "_record_reconciliation_success", record_success)
    monkeypatch.setattr(main_module, "_record_reconciliation_failure", record_failure)
    monkeypatch.setattr(main_module, "_mark_worker_loop_success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "_mark_worker_loop_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "SessionLocal", lambda: _ProbeFailureSession())

    with pytest.raises(_StopLoop):
        asyncio.run(main_module._reconciliation_loop())

    assert session_executes["count"] >= 2  # first probe fails; customer scope still queries
    assert any(
        scope == "crypto" and "unresolved-order probe" in message
        for scope, message in failures
    )
    assert ("reconcile", "oanda", None) in calls
    assert "oanda" in successes
    assert "customer-live" in successes


def test_oanda_practice_reconciliation_selects_forex_demo_pending_and_unknown_orders(monkeypatch):
    captured = {"queries": []}
    cursor = SimpleNamespace(
        last_transaction_id="100",
        status="READY",
        last_error="",
        last_sync_at=None,
    )

    class _QueryResult:
        def __init__(self, *, rows=None, single=None):
            self.rows = rows or []
            self.single = single

        def scalars(self):
            return self

        def all(self):
            return self.rows

        def scalar_one_or_none(self):
            return self.single

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement):
            captured["queries"].append(statement)
            if len(captured["queries"]) == 1:
                return _QueryResult(rows=[])
            return _QueryResult(single=cursor)

        async def commit(self):
            return None

    class _OandaBroker:
        def account_changes(self, transaction_id):
            assert transaction_id == "100"
            return {"lastTransactionID": "101", "changes": {}}

        def account(self):
            return {
                "account": {
                    "balance": "1000",
                    "NAV": "1000",
                    "marginAvailable": "900",
                }
            }

        def close(self):
            return None

    monkeypatch.setattr(execution_module.settings, "oanda_account_id", "practice-account")
    monkeypatch.setattr(execution_module.settings, "oanda_api_token", "practice-token")
    monkeypatch.setattr(execution_module.settings, "oanda_practice", True)
    monkeypatch.setattr(execution_module, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(execution_module, "OandaBroker", lambda *_args, **_kwargs: _OandaBroker())

    async def noop_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(execution_module, "audit", noop_audit)

    result = asyncio.run(execution_module.reconcile("oanda"))
    compiled = captured["queries"][0].compile()
    parameter_values = []
    for value in compiled.params.values():
        parameter_values.extend(value if isinstance(value, (tuple, list, set)) else [value])

    assert "FOREX_DEMO" in parameter_values
    assert not {"LIVE", "PAPER"} & set(parameter_values)
    assert {"PENDING", "OPEN", "PARTIAL", "UNKNOWN"} <= set(parameter_values)
    assert result["account"]["balance"] == "1000"
