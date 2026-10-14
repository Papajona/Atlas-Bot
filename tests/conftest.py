import os
import signal

# Test suite must choose an explicit safe environment rather than relying on
# Settings' production/staging fail-closed behavior.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_trading.db")
os.environ.setdefault("BACKGROUND_RECONCILIATION_ENABLED", "false")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token-for-pytest-only")


import pathlib
import pytest


def _test_timeout_handler(signum, frame):
    raise TimeoutError(
        f"pytest test exceeded {os.environ.get('PYTEST_TEST_TIMEOUT_SECONDS', '120')} seconds"
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Bound a single test call on POSIX CI without adding an unlocked plugin dependency."""
    try:
        seconds = int(os.environ.get("PYTEST_TEST_TIMEOUT_SECONDS", "120"))
    except ValueError:
        seconds = 120
    if seconds <= 0 or not hasattr(signal, "setitimer") or not hasattr(signal, "SIGALRM"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _test_timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


@pytest.fixture(scope="session", autouse=True)
def _fresh_default_test_db():
    """Remove only the default throw-away SQLite DB before a pytest session."""
    if os.environ.get("DATABASE_URL", "").endswith("///./test_trading.db"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            pathlib.Path("test_trading.db" + suffix).unlink(missing_ok=True)
    yield
