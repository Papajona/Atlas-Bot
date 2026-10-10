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


def test_request_json_preserves_sanitized_json_http_error(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", hdrs=None,
            fp=__import__("io").BytesIO(
                b'{"error":{"message":"model access denied","type":"access_error","secret":"do-not-log"}}'
            ),
        )

    monkeypatch.setattr(checker.urllib.request, "urlopen", fake_urlopen)
    status, body = checker._request_json("https://example.invalid/inference", {}, {})

    assert status == 403
    assert body == {"error": {"message": "model access denied", "type": "access_error"}}
    assert "secret" not in repr(body)


def test_request_json_preserves_non_json_http_error_preview(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", hdrs=None,
            fp=__import__("io").BytesIO(b"gateway denied this request"),
        )

    monkeypatch.setattr(checker.urllib.request, "urlopen", fake_urlopen)
    status, body = checker._request_json("https://example.invalid/inference", {}, {})

    assert status == 403
    assert body["response_preview"] == "gateway denied this request"


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


@pytest.mark.parametrize("body", [
    {"candidates": [None]},
    {"candidates": [{"content": []}]},
    {"candidates": [{"content": {"parts": [None, {"text": 123}]}}]},
])
def test_gemini_checker_fails_closed_on_malformed_payload(monkeypatch, body):
    monkeypatch.setattr(checker, "_request_json", lambda *args, **kwargs: (200, body))
    monkeypatch.setattr(checker.settings, "gemini_api_key", "test-key")
    assert checker._check_gemini("test-model") is False


@pytest.mark.parametrize("body", [
    {"choices": [None]},
    {"choices": [{"message": []}]},
    {"choices": [{"message": {"content": {"text": "OK"}}}]},
])
def test_groq_checker_fails_closed_on_malformed_payload(monkeypatch, body):
    monkeypatch.setattr(checker, "_request_json", lambda *args, **kwargs: (200, body))
    monkeypatch.setattr(checker.settings, "groq_api_key", "test-key")
    assert checker._check_groq("test-model") is False



def test_groq_checker_uses_documented_chat_completions_and_parses_text(monkeypatch, capsys):
    calls = []

    def fake_request(url, headers, payload=None):
        calls.append((url, payload))
        if payload is None:
            return 200, {"data": [{"id": "test-model"}]}
        return 200, {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(checker, "_request_json", fake_request)
    monkeypatch.setattr(checker.settings, "groq_api_key", "test-key")

    assert checker._check_groq("test-model") is True
    assert calls[0][0] == "https://api.groq.com/openai/v1/models"
    assert calls[1][0] == "https://api.groq.com/openai/v1/chat/completions"
    assert calls[1][1]["model"] == "test-model"
    assert calls[1][1]["messages"] == [{"role": "user", "content": "Reply with OK."}]
    assert calls[1][1]["max_completion_tokens"] == 1024
    assert "Chat Completions inference succeeded" in capsys.readouterr().out


def test_gemini_checker_uses_nontrivial_output_budget(monkeypatch):
    captured = {}

    def fake_request(url, headers, payload=None):
        captured["payload"] = payload
        return 200, {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}

    monkeypatch.setattr(checker, "_request_json", fake_request)
    monkeypatch.setattr(checker.settings, "gemini_api_key", "test-key")

    assert checker._check_gemini("test-model") is True
    assert captured["payload"]["generationConfig"]["maxOutputTokens"] == 1024


def test_groq_checker_sends_explicit_user_agent(monkeypatch):
    captured = []

    def fake_request(url, headers, payload=None):
        captured.append((url, dict(headers)))
        if payload is None:
            return 200, {"data": [{"id": "test-model"}]}
        return 200, {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(checker, "_request_json", fake_request)
    monkeypatch.setattr(checker.settings, "groq_api_key", "test-key")

    assert checker._check_groq("test-model") is True
    assert len(captured) == 2
    for _, headers in captured:
        assert headers["User-Agent"] == (
            "Atlas-Bot-Production-Verification/3.10.47"
        )
        assert headers["Accept"] == "application/json"
        assert headers["Authorization"] == "Bearer test-key"
