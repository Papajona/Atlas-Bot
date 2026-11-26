"""Lifecycle regression for the isolated customer-identity quarantine pool."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import db, main


def test_identity_quarantine_engine_dispose_releases_its_pool(monkeypatch):
    pool = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(db, "identity_quarantine_engine", pool)

    asyncio.run(db.dispose_identity_quarantine_engine())

    pool.dispose.assert_awaited_once_with()


def test_application_shutdown_disposes_identity_quarantine_engine(monkeypatch):
    dispose = AsyncMock()
    monkeypatch.setattr(main, "dispose_identity_quarantine_engine", dispose)
    monkeypatch.setattr(main, "_worker_tasks", set())
    monkeypatch.setattr(main, "_background_task", None)
    monkeypatch.setattr(main, "_usdt_task", None)

    asyncio.run(main.shutdown())

    dispose.assert_awaited_once_with()
