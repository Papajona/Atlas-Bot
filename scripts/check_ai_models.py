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
        # Preserve only parsed provider error metadata so the gate can explain
        # authorization/model-access failures without logging credentials.
        try:
            raw_error = exc.read().decode("utf-8")
            parsed_error = json.loads(raw_error)
            return exc.code, parsed_error if isinstance(parsed_error, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError, Exception):
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
            "generationConfig": {"maxOutputTokens": 64, "temperature": 0},
        },
    )
    candidates = body.get("candidates")
    text = ""
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        content = candidates[0].get("content")
        parts = content.get("parts") if isinstance(content, dict) else []
        if isinstance(parts, list):
            text = "".join(
                part.get("text", "") for part in parts
                if isinstance(part, dict) and isinstance(part.get("text", ""), str)
            )
    ok = status == 200 and bool(text.strip())
    if ok:
        detail = "generation succeeded"
    else:
        detail = f"generation failed (HTTP {status or 'network/error'})"
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            detail += "; provider_error=" + error["message"][:180]
        prompt_feedback = body.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            detail += "; prompt_block_reason=" + str(prompt_feedback["blockReason"])[:80]
        if status == 200:
            detail += "; candidates=" + str(len(candidates) if isinstance(candidates, list) else 0)
            if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
                finish = candidates[0].get("finishReason")
                if finish:
                    detail += "; finish_reason=" + str(finish)[:80]
    print(f"[{'PASS' if ok else 'FAIL'}] Gemini {model}: {detail}")
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
    choices = body.get("choices")
    message = choices[0].get("message") if (
        isinstance(choices, list) and choices and isinstance(choices[0], dict)
    ) else None
    content = message.get("content") if isinstance(message, dict) else None
    ok = status == 200 and isinstance(content, str) and bool(content.strip())
    if ok:
        detail = "chat completion succeeded"
    else:
        detail = f"chat completion failed (HTTP {status or 'network/error'})"
        error = body.get("error")
        if isinstance(error, dict):
            # Provider error messages help distinguish access policy from model
            # errors; never print request headers or API keys.
            message = error.get("message") or error.get("detail") or error.get("type")
            error_code = error.get("code") or error.get("type")
            if isinstance(error_code, (str, int)):
                detail += "; provider_code=" + str(error_code)[:80]
            if isinstance(message, str):
                detail += "; provider_error=" + message[:180]
        elif isinstance(error, str):
            detail += "; provider_error=" + error[:180]
        elif isinstance(body.get("message"), str):
            detail += "; provider_error=" + body["message"][:180]
        if status == 403 and not any(k in detail for k in ("provider_code=", "provider_error=")):
            detail += "; provider_error_details=unavailable_or_unrecognized_response"
    print(f"[{'PASS' if ok else 'FAIL'}] Groq {model}: {detail}")
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
