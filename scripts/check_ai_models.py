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


def _request_json(url: str, headers: dict[str, str], payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    method = "POST" if payload is not None else "GET"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Preserve only parsed provider error metadata so the gate can explain
        # authorization/model-access failures without logging credentials.
        try:
            raw_error = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:
            return exc.code, {}
        try:
            parsed_error = json.loads(raw_error)
            if isinstance(parsed_error, dict):
                # Keep the standard provider error structure, but bound strings
                # so CI logs cannot be flooded by an upstream response.
                error = parsed_error.get("error")
                if isinstance(error, dict):
                    safe_error = {
                        key: value[:240] if isinstance(value, str) else value
                        for key, value in error.items()
                        if key in {"message", "type", "code", "status", "detail"}
                        and isinstance(value, (str, int, float, bool, type(None)))
                    }
                    return exc.code, {"error": safe_error}
                message = parsed_error.get("message") or parsed_error.get("detail")
                if isinstance(message, str):
                    return exc.code, {"message": message[:240]}
                return exc.code, {"response_preview": raw_error[:240]}
        except json.JSONDecodeError:
            pass
        # Non-JSON responses commonly come from a proxy/gateway; retain only a
        # short sanitized preview, never request headers or credentials.
        return exc.code, {"response_preview": raw_error[:240]}
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
            "generationConfig": {"maxOutputTokens": 1024, "temperature": 0},
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
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Atlas-Bot-Production-Verification/3.10.47",
        "Accept": "application/json",
    }

    # The documented Models List endpoint is a useful diagnostic, but listing
    # failure alone must not mask whether the configured model can actually
    # generate a response.
    list_status, list_body = _request_json(
        "https://api.groq.com/openai/v1/models", headers
    )
    listed = list_body.get("data")
    model_list_ok = list_status == 200 and isinstance(listed, list)
    model_available = model_list_ok and any(
        isinstance(item, dict) and item.get("id") == model for item in listed
    )
    if model_list_ok:
        print(
            f"[{'PASS' if model_available else 'WARN'}] Groq models list: "
            + (f"{model} is listed" if model_available else f"{model} is not listed")
        )
    else:
        detail = f"HTTP {list_status or 'network/error'}"
        error = list_body.get("error")
        if isinstance(error, dict):
            msg = error.get("message") or error.get("detail") or error.get("type")
            code = error.get("code") or error.get("type")
            if isinstance(code, (str, int)):
                detail += "; provider_code=" + str(code)[:80]
            if isinstance(msg, str):
                detail += "; provider_error=" + msg[:180]
        elif isinstance(list_body.get("message"), str):
            detail += "; provider_error=" + list_body["message"][:180]
        elif isinstance(list_body.get("response_preview"), str):
            detail += "; response=" + list_body["response_preview"][:120]
        print(f"[WARN] Groq models list: {detail}; checking inference separately")

    # Use the documented Chat Completions endpoint as the compatibility baseline.
    # max_completion_tokens is preferred over the deprecated max_tokens parameter.
    status, body = _request_json(
        "https://api.groq.com/openai/v1/chat/completions",
        headers,
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_completion_tokens": 1024,
            "temperature": 0,
        },
    )
    output_text = ""
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            value = message.get("content")
            if isinstance(value, str):
                output_text = value
            elif isinstance(value, list):
                output_text = "".join(
                    part.get("text", "") for part in value
                    if isinstance(part, dict) and isinstance(part.get("text", ""), str)
                )
    ok = status == 200 and bool(output_text.strip())
    if ok:
        detail = "Chat Completions inference succeeded"
    else:
        detail = f"Chat Completions inference failed (HTTP {status or 'network/error'})"
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail") or error.get("type")
            error_code = error.get("code") or error.get("type")
            if isinstance(error_code, (str, int)):
                detail += "; provider_code=" + str(error_code)[:80]
            if isinstance(message, str):
                detail += "; provider_error=" + message[:180]
        elif isinstance(body.get("message"), str):
            detail += "; provider_error=" + body["message"][:180]
        elif isinstance(body.get("response_preview"), str):
            detail += "; response=" + body["response_preview"][:120]
        if status == 200:
            choices = body.get("choices")
            detail += "; choices=" + str(len(choices) if isinstance(choices, list) else 0)
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                finish = choices[0].get("finish_reason")
                if finish:
                    detail += "; finish_reason=" + str(finish)[:80]
            usage = body.get("usage")
            if isinstance(usage, dict):
                completion_tokens = usage.get("completion_tokens")
                if isinstance(completion_tokens, int):
                    detail += "; completion_tokens=" + str(completion_tokens)
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
