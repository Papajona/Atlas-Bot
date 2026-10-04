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
