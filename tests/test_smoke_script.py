import pytest

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
    from scripts import smoke

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
