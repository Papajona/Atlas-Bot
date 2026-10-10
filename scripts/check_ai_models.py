#!/usr/bin/env python3
"""Verify configured AI providers with minimal real inference requests.

Both Gemini and Groq must be configured and each configured model must return
usable generated text. Exit 0 = all requests succeeded, 1 = request failed,
2 = required credentials/configuration missing. Never prints keys or response bodies.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Direct execution puts scripts/, not the repository root, at sys.path[0].
_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[1])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from app.config import settings


def _request_json(url: str, headers: dict[str, str], payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception:
        return 0, {}
    try:
        parsed = json.loads(raw)
        return status, parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, {}


def _check_gemini(model: str) -> bool:
    encoded_model = urllib.parse.quote(model, safe="-_.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent"
    status, body = _request_json(
        url,
        {"Content-Type": "application/json", "x-goog-api-key": settings.gemini_api_key},
        {
            "contents": [{"parts": [{"text": "Reply with OK."}]}],
            "generationConfig": {"maxOutputTokens": 8, "temperature": 0},
        },
    )
    candidates = body.get("candidates") or []
    text = ""
    if candidates:
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(str(part.get("text") or "") for part in parts)
    ok = status == 200 and bool(text.strip())
    print(f"[{'PASS' if ok else 'FAIL'}] Gemini {model}: " +
          ("generation succeeded" if ok else f"generation failed (HTTP {status or 'network/error'})"))
    return ok


def _check_groq(model: str) -> bool:
    status, body = _request_json(
        "https://api.groq.com/openai/v1/chat/completions",
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.groq_api_key}",
        },
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "temperature": 0,
        },
    )
    choices = body.get("choices") or []
    message = choices[0].get("message") if choices else None
    ok = status == 200 and isinstance(message, dict) and bool(str(message.get("content") or "").strip())
    print(f"[{'PASS' if ok else 'FAIL'}] Groq {model}: " +
          ("chat completion succeeded" if ok else f"chat completion failed (HTTP {status or 'network/error'})"))
    return ok


def main() -> int:
    missing = []
    if not settings.gemini_api_key:
        missing.append("GEMINI_API_KEY")
    if not settings.groq_api_key:
        missing.append("GROQ_API_KEY")
    if missing:
        print("AI provider verification: NOT VERIFIED (missing " + ", ".join(missing) + ").")
        return 2

    models = (
        ("Gemini", sorted({settings.gemini_model, settings.gemini_strategy_model} - {""}), _check_gemini),
        ("Groq", sorted({settings.groq_model, settings.groq_strategy_model} - {""}), _check_groq),
    )
    checked = 0
    failures = 0
    for provider, provider_models, checker in models:
        if not provider_models:
            print(f"AI provider verification: NOT VERIFIED ({provider} has no configured model).")
            return 2
        for model in provider_models:
            checked += 1
            failures += not checker(model)
    if not checked:
        print("AI provider verification: NOT VERIFIED (no model checks were configured).")
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
