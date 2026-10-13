from __future__ import annotations

import asyncio
from types import SimpleNamespace

import app.main as main
from app.config import settings


def test_repeated_critical_worker_failures_halt_and_open_incident(monkeypatch):
    name = "test-critical-loop"
    main._CRITICAL_WORKER_FAILURES.pop(name, None)
    main._CRITICAL_WORKER_HALT_ISSUED.discard(name)
    monkeypatch.setattr(settings, "critical_worker_failure_threshold", 2)

    state = SimpleNamespace(kill_switch=False, live_enabled=True, mode="PAPER")
    account = SimpleNamespace(status="ACTIVE")
    incidents = []

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return [account]

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, model, key):
            return state

        async def execute(self, query):
            return _Rows()

        async def commit(self):
            return None

    async def record_incident(**kwargs):
        incidents.append(kwargs)

    monkeypatch.setattr(main, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(main, "open_incident", record_incident)

    async def run():
        await main._handle_critical_worker_failure(name, RuntimeError("first"))
        assert state.kill_switch is False
        assert incidents == []
        await main._handle_critical_worker_failure(name, RuntimeError("second"))

    asyncio.run(run())

    assert state.kill_switch is True
    assert state.live_enabled is False
    assert state.mode == "HALTED"
    assert account.status == "HALTED"
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "CRITICAL"
    assert incidents[0]["detail"]["consecutive_failures"] == 2
    assert main._CRITICAL_WORKER_FAILURES[name] == 2

    # A later failure must not repeatedly rewrite the same halt or spam incidents.
    asyncio.run(main._handle_critical_worker_failure(name, RuntimeError("third")))
    assert len(incidents) == 1

    main._CRITICAL_WORKER_FAILURES.pop(name, None)
    main._CRITICAL_WORKER_HALT_ISSUED.discard(name)


def test_critical_worker_success_resets_consecutive_failure_streak():
    name = "test-critical-loop-reset"
    main._CRITICAL_WORKER_FAILURES[name] = 2
    main._mark_critical_worker_success(name)
    assert main._CRITICAL_WORKER_FAILURES[name] == 0
    assert main._worker_loop_health[name]["status"] == "RUNNING"
    main._CRITICAL_WORKER_FAILURES.pop(name, None)
