"""Safety tests for the disposable release-verification database guard."""
import pytest

from scripts.verify_release_database_guard import is_approved_verification_database_url


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+aiosqlite:///./data/verify.db",
        "postgresql+asyncpg://atlas:test@localhost:5432/verify",
        "postgresql+asyncpg://atlas:test@127.0.0.1:5432/verify",
        "postgresql+asyncpg://atlas:test@[::1]:5432/verify",
    ],
)
def test_release_guard_accepts_sqlite_and_loopback_databases(database_url):
    assert is_approved_verification_database_url(database_url)


def test_release_guard_accepts_only_declared_compose_postgres_inside_container():
    url = "postgresql+asyncpg://atlas:test@postgres:5432/verify"
    assert is_approved_verification_database_url(
        url, compose_service_host="postgres", in_container=True
    )
    assert not is_approved_verification_database_url(
        url, compose_service_host="", in_container=True
    )
    assert not is_approved_verification_database_url(
        url, compose_service_host="postgres", in_container=False
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://atlas:test@db.example.com:5432/verify",
        "postgresql+asyncpg://atlas:test@203.0.113.10:5432/verify",
        "postgresql+asyncpg://atlas:test@postgres.example.com:5432/verify",
        "mysql+pymysql://atlas:test@localhost:3306/verify",
        "",
    ],
)
def test_release_guard_rejects_remote_or_unapproved_databases(database_url):
    assert not is_approved_verification_database_url(
        database_url, compose_service_host="postgres", in_container=True
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://atlas:test@localhost:5432/verify?host=db.example.com",
        "postgresql+asyncpg://atlas:test@127.0.0.1:5432/verify?hostaddr=203.0.113.10",
        "postgresql+asyncpg://atlas:test@postgres:5432/verify?host=remote.example.com",
        "sqlite:///./verify.db?host=remote.example.com",
    ],
)
def test_release_guard_rejects_driver_level_host_overrides(database_url):
    assert not is_approved_verification_database_url(
        database_url, compose_service_host="postgres", in_container=True
    )


def test_release_guard_does_not_allow_remote_host_in_multi_host_override():
    url = (
        "postgresql+asyncpg://atlas:test@localhost:5432/verify"
        "?host=localhost:5432&host=db.example.com:5432"
    )
    assert not is_approved_verification_database_url(url)
