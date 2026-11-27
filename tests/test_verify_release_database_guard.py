"""Compatibility and safety tests for the disposable release-verification database guard."""
import pytest

from scripts.normalize_asyncpg_database_url import normalize_asyncpg_database_url
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


def test_release_script_enforces_database_guard_before_mutating_database():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "verify_release.sh").read_text(encoding="utf-8")
    guard = script.index("python scripts/verify_release_database_guard.py")
    migration = script.index("python -m alembic upgrade head")
    postgres_tests = script.index("tests/test_postgres_concurrency_3_10_39.py")
    assert guard < migration < postgres_tests


def test_compose_verification_declares_the_exact_postgres_service_alias():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.verify.yml").read_text(encoding="utf-8")
    assert "ATLAS_VERIFY_COMPOSE_POSTGRES_SERVICE: postgres" in compose


def test_live_money_runtime_verifier_requires_loopback_proxy_before_alembic():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = (root / "deploy" / "verify-live-money-runtime.sh").read_text(encoding="utf-8")
    normalize = script.index("printf '%s' " + '"$DATABASE_URL"' + " | python scripts/normalize_asyncpg_database_url.py")
    assert 'python scripts/normalize_asyncpg_database_url.py "$DATABASE_URL"' not in script
    assert "env -u DATABASE_URL python scripts/normalize_asyncpg_database_url.py" in script
    export_url = script.index("export DATABASE_URL")
    guard = script.index("python scripts/verify_release_database_guard.py")
    migration = script.index("python -m alembic upgrade head")
    assert "unset ATLAS_VERIFY_COMPOSE_POSTGRES_SERVICE" in script
    assert "loopback-bound Cloud SQL Auth Proxy/tunnel" in script
    assert normalize < export_url < guard < migration


def test_live_money_runtime_verifier_normalizes_legacy_postgres_urls_for_async_alembic():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = (root / "deploy" / "verify-live-money-runtime.sh").read_text(encoding="utf-8")
    docs = (root / "deploy" / "DEPLOY_WORKFLOW.md").read_text(encoding="utf-8")

    normalize = script.index("printf '%s' " + '"$DATABASE_URL"' + " | python scripts/normalize_asyncpg_database_url.py")
    assert 'python scripts/normalize_asyncpg_database_url.py "$DATABASE_URL"' not in script
    export_url = script.index("export DATABASE_URL")
    guard = script.index("python scripts/verify_release_database_guard.py")
    migration = script.index("python -m alembic upgrade head")

    assert normalize < export_url < guard < migration
    assert "DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@127.0.0.1:5432/DB" in docs
    assert "DATABASE_URL=postgresql://" not in docs


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        (
            "postgresql://atlas:secret@127.0.0.1:5432/verify?sslmode=require",
            "postgresql+asyncpg://atlas:secret@127.0.0.1:5432/verify?ssl=require",
        ),
        (
            "postgres://atlas:secret@localhost:5432/verify?sslmode=verify-full&application_name=atlas",
            "postgresql+asyncpg://atlas:secret@localhost:5432/verify?ssl=verify-full&application_name=atlas",
        ),
        (
            "POSTGRESQL://atlas:secret@127.0.0.1:5432/verify?sslmode=require",
            "postgresql+asyncpg://atlas:secret@127.0.0.1:5432/verify?ssl=require",
        ),
        (
            "POSTGRESQL+ASYNCPG://atlas:secret@localhost:5432/verify?ssl=prefer",
            "postgresql+asyncpg://atlas:secret@localhost:5432/verify?ssl=prefer",
        ),
        (
            "postgresql+asyncpg://atlas:secret@localhost:5432/verify?ssl=prefer",
            "postgresql+asyncpg://atlas:secret@localhost:5432/verify?ssl=prefer",
        ),
    ],
)
def test_asyncpg_database_url_normalizer_preserves_ssl_mode(database_url, expected):
    assert normalize_asyncpg_database_url(database_url) == expected


@pytest.mark.parametrize("sslmode", ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"])
def test_asyncpg_database_url_normalizer_maps_all_libpq_ssl_modes(sslmode):
    normalized = normalize_asyncpg_database_url(
        f"postgresql://atlas:secret@localhost/verify?sslmode={sslmode}"
    )
    assert normalized.endswith(f"?ssl={sslmode}")


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://atlas:secret@localhost/verify?sslmode=require&ssl=disable",
        "postgresql://atlas:secret@localhost/verify?sslmode=require&sslmode=disable",
        "postgresql://atlas:secret@localhost/verify?sslmode=invalid",
        "mysql://atlas:secret@localhost/verify?sslmode=require",
    ],
)
def test_asyncpg_database_url_normalizer_rejects_ambiguous_or_unsupported_ssl(database_url):
    with pytest.raises(ValueError):
        normalize_asyncpg_database_url(database_url)


def test_sslmode_normalization_keeps_loopback_database_guard_approval():
    url = normalize_asyncpg_database_url(
        "postgresql://atlas:secret@127.0.0.1:5432/verify?sslmode=require"
    )
    assert url.endswith("?ssl=require")
    assert is_approved_verification_database_url(url)

def test_asyncpg_database_url_normalizer_cli_reads_secret_from_stdin():
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "normalize_asyncpg_database_url.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        input="POSTGRESQL://atlas:secret@127.0.0.1:5432/verify?sslmode=require",
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == (
        "postgresql+asyncpg://atlas:secret@127.0.0.1:5432/verify?ssl=require"
    )
