import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_ai_models_under_test", ROOT / "scripts" / "check_ai_models.py"
)
checker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(checker)


class FakeResponse:
    def __init__(self, status=200, body='{"ok": true}'):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body.encode("utf-8")


def test_request_json_posts_json_and_parses_object(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse(body='{"choices": [{"message": {"content": "OK"}}]}')

    monkeypatch.setattr(checker.urllib.request, "urlopen", fake_urlopen)
    status, body = checker._request_json(
        "https://example.invalid/inference",
        {"Content-Type": "application/json", "Authorization": "Bearer test"},
        {"model": "test-model"},
    )

    assert status == 200
    assert body["choices"][0]["message"]["content"] == "OK"
    assert captured["method"] == "POST"
    assert captured["payload"] == {"model": "test-model"}
    assert captured["timeout"] == 20


def test_request_json_reports_http_status_without_exposing_body(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", hdrs=None, fp=None
        )

    monkeypatch.setattr(checker.urllib.request, "urlopen", fake_urlopen)
    status, body = checker._request_json(
        "https://example.invalid/inference", {}, {}
    )

    assert status == 403
    assert body == {}


@pytest.mark.parametrize("raw", ["not-json", "[]", ""])
def test_request_json_rejects_invalid_or_non_object_json(monkeypatch, raw):
    monkeypatch.setattr(
        checker.urllib.request, "urlopen",
        lambda request, timeout: FakeResponse(body=raw),
    )
    status, body = checker._request_json(
        "https://example.invalid/inference", {}, {}
    )

    assert status == 200
    assert body == {}
