"""Regression for a worker whose audit persistence fails."""
import asyncio
import uuid

import pytest


def test_supervisor_restarts_worker_after_audit_failure(monkeypatch):
    import app.main as main

    worker_name = f"supervisor-regression-{uuid.uuid4().hex}"
    calls = []

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", no_wait)
    monkeypatch.setitem(main._worker_loop_health, worker_name, {"restart_count": 0})

    async def broken_audit(*args, **kwargs):
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(main, "_audit", broken_audit)

    async def worker():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            await main._audit("WORKER_TEST_FAILURE", {"cycle": 1})
        raise asyncio.CancelledError()

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await main._supervise_worker_loop(worker, worker_name)

    asyncio.run(run())

    assert calls == [1, 2]
    assert main._worker_loop_health[worker_name]["restart_count"] == 1
    assert main._worker_loop_health[worker_name]["status"] == "STOPPED"
