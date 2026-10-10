#!/usr/bin/env python3
"""Fail fast when a configured LLM model id has been retired by its provider (run in CI nightly and before every deploy).

Gemini: GET /v1beta/models/<id>   Groq: GET /openai/v1/models   (needs GEMINI_API_KEY / GROQ_API_KEY; skips a provider whose key is unset).
Exit 0 = every configured id is served, 1 = at least one id is missing/unreachable, 2 = nothing could be checked.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# When Python executes a file as "python scripts/check_ai_models.py", sys.path[0]
# is the scripts directory, not necessarily the repository root. Bootstrap the
# project root before importing the application package so CI and local invocations
# behave consistently regardless of the caller's working directory.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from app.config import settings


def _get(url: str, headers: dict) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # network failure is a FAIL, not a pass
        return 0, repr(e)


def main() -> int:
    checked, bad = 0, 0
    if settings.gemini_api_key:
        for model in sorted({settings.gemini_model, settings.gemini_strategy_model} - {""}):
            code, body = _get(f"https://generativelanguage.googleapis.com/v1beta/models/{model}", {"x-goog-api-key": settings.gemini_api_key})
            checked += 1
            ok = code == 200
            bad += 0 if ok else 1
            print(f"[{'OK' if ok else 'FAIL'}] gemini {model} -> HTTP {code}")
    if settings.groq_api_key:
        code, body = _get("https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {settings.groq_api_key}"})
        if code != 200:
            # Do not misreport authorization/network failures as a retired model ID.
            # The caller must fix provider access before model availability can be assessed.
            checked += 1
            bad += 1
            print(f"[FAIL] groq models endpoint -> HTTP {code}; cannot verify configured model IDs")
        else:
            try:
                served = {m.get("id") for m in json.loads(body).get("data", [])}
            except (ValueError, TypeError):
                served = set()
                code = 200
            for model in sorted({settings.groq_model, settings.groq_strategy_model} - {""}):
                checked += 1
                ok = model in served
                bad += 0 if ok else 1
                print(f"[{'OK' if ok else 'FAIL'}] groq {model} -> {'served' if ok else 'not listed (HTTP 200)'}")
    if not checked:
        print("No provider keys set; nothing checked.")
        return 2
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
