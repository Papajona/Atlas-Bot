import os

# Test suite must choose an explicit safe environment rather than relying on
# Settings' production/staging fail-closed behavior.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_trading.db")
os.environ.setdefault("BACKGROUND_RECONCILIATION_ENABLED", "false")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token-for-pytest-only")


import pathlib
import pytest


@pytest.fixture(scope="session", autouse=True)
def _fresh_default_test_db():
    """Remove only the default throw-away SQLite DB before a pytest session."""
    if os.environ.get("DATABASE_URL", "").endswith("///./test_trading.db"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            pathlib.Path("test_trading.db" + suffix).unlink(missing_ok=True)
    yield


@pytest.fixture(autouse=True)
def _reset_shared_test_settings(monkeypatch):
    """Reset process-wide Settings before every test.

    Individual tests may still override settings with their own monkeypatch
    calls. Resetting here prevents one test's direct Settings mutation from
    changing the behavior of a later test.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///./test_trading.db")
    monkeypatch.setattr(settings, "background_reconciliation_enabled", False)
    monkeypatch.setattr(settings, "admin_token", "test-admin-token-for-pytest-only")
    yield
