import hashlib
import hmac

import pytest

from scripts import smoke
from scripts.smoke import validate_base_url


def test_smoke_check_accepts_https_and_loopback_http():
    assert validate_base_url("https://api.example.test/") == "https://api.example.test"
    assert validate_base_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert validate_base_url("http://localhost:8000") == "http://localhost:8000"


def test_smoke_check_trims_whitespace_before_validating_and_returning_url():
    assert validate_base_url("  https://api.example.test/path/  ") == "https://api.example.test/path"


@pytest.mark.parametrize(
    ("url", "connection_name", "expected_port"),
    [
        ("http://[::1]/", "HTTPConnection", 80),
        ("https://[2001:db8::1]/", "HTTPSConnection", 443),
    ],
)
def test_smoke_connection_sets_default_port_for_ipv6_hosts(
    monkeypatch, url, connection_name, expected_port
):
    from urllib.parse import urlsplit

    captured = {}

    class FakeConnection:
        def __init__(self, host, *, port, timeout, **_kwargs):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout

    monkeypatch.setattr(smoke.http.client, connection_name, FakeConnection)
    connection = smoke._connection(urlsplit(url))
    assert isinstance(connection, FakeConnection)
    assert captured["host"] in {"::1", "2001:db8::1"}
    assert captured["port"] == expected_port
    assert captured["timeout"] == 5


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/passwd",
        "ftp://example.test",
        "http://example.test",
        "http://user:password@127.0.0.1:8000",
        "https:///missing-host",
        "https://example.test/path?token=secret",
    ],
)
def test_smoke_check_rejects_unsafe_or_ambiguous_base_urls(value):
    with pytest.raises(ValueError):
        validate_base_url(value)


def test_paper_readiness_token_matches_paper_staging_contract(tmp_path):
    database_url = "postgresql+asyncpg://paper-user:paper-password@db.example.test/postgres"
    database_url_file = tmp_path / "paper-database-url"
    database_url_file.write_text(database_url + "\n", encoding="utf-8")

    expected = hmac.new(
        database_url.encode("utf-8"),
        b"atlas-paper-staging-readiness-v1",
        hashlib.sha256,
    ).hexdigest()
    assert smoke.paper_readiness_token_from_file(database_url_file) == expected


def test_smoke_check_authenticates_paper_readiness_and_skips_unavailable_metrics(
    monkeypatch, tmp_path, capsys
):
    database_url = "postgresql+asyncpg://paper-user:do-not-print@db.example.test/postgres"
    database_url_file = tmp_path / "paper-database-url"
    database_url_file.write_text(database_url, encoding="utf-8")
    requests = []

    class FakeResponse:
        status = 200

        @staticmethod
        def read(_limit):
            return b'{"status":"ok"}'

    class FakeConnection:
        def request(self, method, path, *, headers=None):
            requests.append((method, path, dict(headers or {})))

        @staticmethod
        def getresponse():
            return FakeResponse()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(smoke, "_connection", lambda _parsed: FakeConnection())
    result = smoke.main([
        "https://paper-staging.example.test",
        "--paper-database-url-file",
        str(database_url_file),
        "--skip-metrics",
    ])

    expected_token = smoke.paper_readiness_token_from_file(database_url_file)
    assert result == 0
    assert [path for _method, path, _headers in requests] == ["/healthz", "/readyz"]
    assert requests[0][2] == {}
    assert requests[1][2] == {"Authorization": f"Bearer {expected_token}"}
    output = capsys.readouterr().out
    assert "do-not-print" not in output
    assert expected_token not in output


def test_smoke_check_authenticates_admin_metrics_from_protected_file(
    monkeypatch, tmp_path
):
    admin_token_file = tmp_path / "admin-token"
    admin_token_file.write_text("test-admin-token\n", encoding="utf-8")
    requests = []

    class FakeResponse:
        status = 200

        @staticmethod
        def read(_limit):
            return b"ok"

    class FakeConnection:
        def request(self, method, path, *, headers=None):
            requests.append((method, path, dict(headers or {})))

        @staticmethod
        def getresponse():
            return FakeResponse()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(smoke, "_connection", lambda _parsed: FakeConnection())
    assert smoke.main([
        "https://api.example.test",
        "--admin-token-file",
        str(admin_token_file),
    ]) == 0

    assert [path for _method, path, _headers in requests] == [
        "/healthz",
        "/readyz",
        "/metrics",
    ]
    assert requests[0][2] == {}
    assert requests[1][2] == {}
    assert requests[2][2] == {"Authorization": "Bearer test-admin-token"}


def test_smoke_check_rejects_empty_secret_files_without_exposing_values(tmp_path):
    empty_file = tmp_path / "empty-secret"
    empty_file.write_text("  \n", encoding="utf-8")

    with pytest.raises(ValueError, match="paper database URL file is empty"):
        smoke.paper_readiness_token_from_file(empty_file)
