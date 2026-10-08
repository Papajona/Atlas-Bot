import os
import pytest
pytest.importorskip("aiosqlite")
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_trading.db"
os.environ["BACKGROUND_RECONCILIATION_ENABLED"] = "false"
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token-for-pytest-only")

from fastapi.testclient import TestClient
from app.main import app
from app.config import settings


@pytest.fixture(autouse=True)
def _development_test_settings(monkeypatch):
    # app.config.settings is a process-wide singleton and may have been
    # imported by earlier tests before this module is collected. Setting the
    # environment variable above does not retroactively change that object.
    # Keep these API tests deterministic without changing production behavior.
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///./test_trading.db")


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
