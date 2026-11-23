import pytest

from scripts.smoke import validate_base_url


def test_smoke_check_accepts_https_and_loopback_http():
    assert validate_base_url("https://api.example.test/") == "https://api.example.test"
    assert validate_base_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert validate_base_url("http://localhost:8000") == "http://localhost:8000"


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
