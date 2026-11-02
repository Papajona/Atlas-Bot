"""Behavioral tests for production MFA and live-approval expiry controls."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import Settings, settings
from app import main


def test_production_admin_mfa_cannot_be_disabled():
    with pytest.raises(ValidationError, match="ADMIN_TOTP_REQUIRED must remain true in production"):
        Settings(_env_file=None, environment="production", admin_totp_required=False)


def test_live_enable_request_ttl_must_be_positive():
    with pytest.raises(ValidationError, match="LIVE_ENABLE_REQUEST_TTL_MINUTES must be >= 1"):
        Settings(_env_file=None, environment="test", live_enable_request_ttl_minutes=0)


def test_expired_live_approval_is_denied_before_broker_preflight(monkeypatch):
    state = SimpleNamespace(
        kill_switch=False,
        live_enable_requested_by="requester-user",
        live_enable_requested_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        live_enabled=False,
        mode="PAPER",
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _model, _id):
            return state

    async def fake_auth(*_args, **_kwargs):
        return {"sub": "approver-user"}

    async def fake_require_role(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "SessionLocal", lambda: Session())
    monkeypatch.setattr(main, "auth", fake_auth)
    monkeypatch.setattr(main, "require_role", fake_require_role)
    monkeypatch.setattr(settings, "paper_trading", False)
    monkeypatch.setattr(settings, "live_trading_enabled", True)
    monkeypatch.setattr(settings, "broker_sandbox", False)
    monkeypatch.setattr(settings, "live_confirmation_text", "ENABLE_LIVE_TRADING")
    monkeypatch.setattr(settings, "live_enable_request_ttl_minutes", 15)

    request = main.LiveEnableRequest(action="APPROVE", confirmation="ENABLE_LIVE_TRADING")
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.enable_live(request))

    assert error.value.status_code == 409
    assert "request has expired" in str(error.value.detail)
    assert state.live_enabled is False
    assert state.mode == "PAPER"
