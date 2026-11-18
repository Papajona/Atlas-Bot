"""Regression tests for API state availability guards."""
import asyncio

import pytest

from app import main


def test_state_endpoint_returns_503_when_application_state_is_missing(monkeypatch):
    class EmptySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, model, key):
            return None

        async def execute(self, statement):
            raise AssertionError("positions must not be queried without provisioned AppState")

    async def allow_admin(*_args, **_kwargs):
        return {"sub": "test-admin"}

    monkeypatch.setattr(main, "auth", allow_admin)
    monkeypatch.setattr(main, "SessionLocal", lambda: EmptySession())

    with pytest.raises(main.HTTPException) as captured:
        asyncio.run(main.state(None, None))

    assert captured.value.status_code == 503
    assert captured.value.detail == "Application state has not been provisioned"
