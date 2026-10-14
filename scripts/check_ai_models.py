#!/usr/bin/env python3
"""Verify configured AI providers with minimal real inference requests.

Both Gemini and Groq must be configured and each configured model must return
usable generated text. Exit 0 = all requests succeeded, 1 = request failed,
2 = required credentials/configuration missing. Never prints keys or response bodies.
"""
import json
import sys
import time
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
    """Request JSON and retry transient provider/network failures a bounded number of times."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    method = "POST" if payload is not None else "GET"
    transient_statuses = {429, 500, 502, 503, 504}
    for attempt in range(3):
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                status = response.status
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                raw_error = exc.read(4096).decode("utf-8", errors="replace")
            except Exception:
                raw_error = ""
            if exc.code in transient_statuses and attempt < 2:
                # Honor a short Retry-After but bound wait time so release verification
                # never sleeps indefinitely on an unhealthy provider.
                try:
                    retry_after = float(exc.headers.get("Retry-After", ""))
                    delay = min(5.0, max(1.0, retry_after))
                except (AttributeError, TypeError, ValueError):
                    delay = 1.5 * (attempt + 1)
                time.sleep(delay)
                continue
            try:
                parsed_error = json.loads(raw_error)
                if isinstance(parsed_error, dict):
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
            # Keep only a short sanitized preview; never log request headers or credentials.
            return exc.code, {"response_preview": raw_error[:240]}
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return 0, {}
        try:
            parsed = json.loads(raw)
            return status, parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return status, {}
    return 0, {}


def _select_gemini_flash_alternatives(models: list[dict], configured_model: str) -> list[str]:
    """Select at most two currently listed stable Flash model IDs for failure diagnostics."""
    import re

    candidates: list[tuple[int, int, int, str]] = []
    configured = configured_model.removeprefix("models/")
    for item in models:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        methods = item.get("supportedGenerationMethods")
        if not isinstance(name, str) or not isinstance(methods, list) or "generateContent" not in methods:
            continue
        model_id = name.removeprefix("models/")
        if model_id == configured or "preview" in model_id.lower():
            continue
        match = re.fullmatch(r"gemini-(\\d+)\\.(\\d+)-flash(-lite)?", model_id)
        if not match:
            continue
        major, minor = int(match.group(1)), int(match.group(2))
        # Prefer the non-lite model at the same version, then newer versions.
        candidates.append((major, minor, 0 if match.group(3) else 1, model_id))
    candidates.sort(reverse=True)
    return [item[3] for item in candidates[:2]]


def _diagnose_gemini_flash_alternatives(configured_model: str) -> None:
    """After a configured model fails, probe up to two catalogue-listed stable Flash alternatives.

    This diagnostic never converts a failed configured-model check into success. It only
    reports a candidate that actually generated text with the configured API credential.
    """
    status, body = _request_json(
        "https://generativelanguage.googleapis.com/v1beta/models",
        {"x-goog-api-key": settings.gemini_api_key, "Accept": "application/json"},
    )
    listed = body.get("models")
    if status != 200 or not isinstance(listed, list):
        print(f"[DIAGNOSTIC] Gemini model catalogue unavailable (HTTP {status or 'network/error'}); no alternative inferred.")
        return
    alternatives = _select_gemini_flash_alternatives(listed, configured_model)
    if not alternatives:
        print("[DIAGNOSTIC] Gemini catalogue returned no stable alternative Flash IDs; no alternative inferred.")
        return
    for alternative in alternatives:
        encoded_model = urllib.parse.quote(alternative, safe="-_.")
        alt_status, alt_body = _request_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{encoded_model}:generateContent",
            {"Content-Type": "application/json", "x-goog-api-key": settings.gemini_api_key},
            {
                "contents": [{"parts": [{"text": "Reply with OK."}]}],
                "generationConfig": {"maxOutputTokens": 32, "temperature": 0},
            },
        )
        candidates = alt_body.get("candidates")
        text = ""
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            content = candidates[0].get("content")
            parts = content.get("parts") if isinstance(content, dict) else []
            if isinstance(parts, list):
                text = "".join(
                    part.get("text", "") for part in parts
                    if isinstance(part, dict) and isinstance(part.get("text", ""), str)
                )
        print(f"[DIAGNOSTIC {'PASS' if alt_status == 200 and text.strip() else 'FAIL'}] Gemini alternative {alternative}: "
              + ("inference succeeded; configure and rerun the gate before release."
                 if alt_status == 200 and text.strip()
                 else f"generation failed (HTTP {alt_status or 'network/error'}); not recommended."))


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


def _check_cerebras(model: str) -> bool:
    """Minimal real inference check; never logs keys or generated response bodies."""
    if not settings.cerebras_api_key:
        print("[SKIP] Cerebras: CEREBRAS_API_KEY not configured (optional provider)")
        return True
    status, body = _request_json(
        "https://api.cerebras.ai/v1/chat/completions",
        {
            "Authorization": f"Bearer {settings.cerebras_api_key}",
            "Content-Type": "application/json",
        },
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_completion_tokens": 32,
            "temperature": 0,
        },
    )
    choices = body.get("choices")
    output = ""
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            output = message["content"]
    ok = status == 200 and bool(output.strip())
    detail = "Chat Completions inference succeeded" if ok else f"Chat Completions inference failed (HTTP {status or 'network/error'})"
    error = body.get("error")
    if not ok and isinstance(error, dict) and isinstance(error.get("message"), str):
        detail += "; provider_error=" + error["message"][:180]
    print(f"[{'PASS' if ok else 'FAIL'}] Cerebras {model}: {detail}")
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

    models = [
        ("Gemini", sorted({settings.gemini_model, settings.gemini_strategy_model} - {""}), _check_gemini),
        ("Groq", sorted({settings.groq_model, settings.groq_strategy_model} - {""}), _check_groq),
    ]
    if settings.cerebras_api_key:
        models.append(("Cerebras", sorted({settings.cerebras_model} - {""}), _check_cerebras))
    checked = 0
    failures = 0
    for provider, provider_models, checker in models:
        if not provider_models:
            print(f"AI provider verification: NOT VERIFIED ({provider} has no configured model).")
            return 2
        for model in provider_models:
            checked += 1
            model_ok = checker(model)
            if not model_ok and provider == "Gemini":
                _diagnose_gemini_flash_alternatives(model)
            failures += not model_ok
    if not checked:
        print("AI provider verification: NOT VERIFIED (no model checks were configured).")
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
