import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
pytest.importorskip("aiosqlite")
os.environ["ENVIRONMENT"] = "development"
# Keep the forced throw-away SQLite URL but isolate each pytest-xdist worker.
_worker = os.environ.get("PYTEST_XDIST_WORKER", "")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./test_trading{'_' + _worker if _worker else ''}.db"
os.environ["BACKGROUND_RECONCILIATION_ENABLED"] = "false"
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token-for-pytest-only")

from fastapi.testclient import TestClient
from app import main as main_module
from app.main import app


def test_health_and_readiness_start():
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        r = client.get("/readyz")
        assert r.status_code == 200
        data = r.json()
        assert "checks" in data
        # 3.10+ renamed the paper-mode readiness check to "safe_trading_config"
        # (true whenever paper trading is on, or live trading is fully and
        # explicitly configured). Development/test env defaults to paper mode.
        assert data["checks"]["safe_trading_config"] is True


def test_metrics_endpoint_exists():
    with TestClient(app) as client:
        # /metrics is admin-gated by default (metrics_require_admin=True) since
        # Prometheus output can leak operational detail; unauthenticated access
        # must be rejected.
        r = client.get("/metrics")
        assert r.status_code == 401
        r = client.get("/metrics", headers={"x-admin-token": os.environ.get("ADMIN_TOKEN", "")})
        assert r.status_code == 200
        assert b"trader_http_requests_total" in r.content



class _CustomerQueryResult:
    def __init__(self, profile):
        self.profile = profile

    def scalar_one_or_none(self):
        return self.profile

    def all(self):
        # _legacy_customer_email_conflict queries for other unmapped legacy users.
        # This fixture represents the no-conflict case.
        return []


class _CustomerIdentityDatabase:
    def __init__(self, profile):
        self.profile = profile

    async def execute(self, _statement):
        return _CustomerQueryResult(self.profile)

    async def flush(self):
        return None


def test_existing_active_profile_email_backfill_rejects_unconfirmed_auth_email(monkeypatch):
    auth_id = "11111111-2222-4333-8444-555555555555"
    profile = SimpleNamespace(
        auth_user_id=auth_id,
        email="",
        display_name="Existing customer",
        status="ACTIVE",
    )
    db = _CustomerIdentityDatabase(profile)

    async def verified_claims(_authorization, *, require_aal2=True):
        return {"sub": auth_id, "email": ""}

    async def auth_user(_token):
        return {
            "id": auth_id,
            "email": "unconfirmed@example.test",
            "email_confirmed_at": None,
            "user_metadata": {},
        }

    monkeypatch.setattr(main_module, "customer_claims", verified_claims)
    monkeypatch.setattr(main_module, "_supabase_user", auth_user)
    monkeypatch.setattr(main_module, "_bearer_token", lambda _authorization: "test-token")

    with pytest.raises(HTTPException) as raised:
        asyncio.run(main_module.get_customer("Bearer test-token", db))

    assert raised.value.status_code == 403
    assert "confirmed email" in raised.value.detail.lower()


def test_active_profile_with_persisted_email_verifies_missing_jwt_email_with_auth(monkeypatch):
    auth_id = "11111111-2222-4333-8444-555555555555"
    profile = SimpleNamespace(
        auth_user_id=auth_id,
        email="existing@example.test",
        display_name="Existing customer",
        status="ACTIVE",
    )
    db = _CustomerIdentityDatabase(profile)

    async def verified_claims(_authorization, *, require_aal2=True):
        return {"sub": auth_id, "email": ""}

    auth_calls = []

    async def auth_user(_token):
        auth_calls.append(_token)
        return {
            "id": auth_id,
            "email": "existing@example.test",
            "email_confirmed_at": "2026-11-25T00:00:00Z",
            "user_metadata": {"full_name": "Existing customer"},
        }

    async def ensure_subscription(_db, _profile):
        return None

    monkeypatch.setattr(main_module, "customer_claims", verified_claims)
    monkeypatch.setattr(main_module, "_supabase_user", auth_user)
    monkeypatch.setattr(main_module, "_ensure_customer_subscription", ensure_subscription)

    returned_profile, _claims = asyncio.run(
        main_module.get_customer("Bearer test-token", db)
    )

    assert auth_calls == ["test-token"]
    assert returned_profile is profile
