"""Optional Groq research review, separate from the local Atlas Native AI engine.

Groq may annotate computed research for audit purposes. Its output is never a trade
instruction, never mutates the deterministic research packet, and never has execution
authority. Strategy composition and trade-safety decisions remain local and deterministic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

import httpx

from .config import settings
from .prompt_safety import UNTRUSTED_PREAMBLE, suspicious_instruction_markers, wrap_untrusted


_LOGGER = logging.getLogger(__name__)
_GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
_SYSTEM_PROMPT = (
    "You are Atlas's optional Groq research reviewer. Review supplied research evidence only. "
    "Do not place trades, provide buy/sell instructions, alter order parameters, invent market data, "
    "or claim execution authority. Return one JSON object only."
)


class GroqAIError(RuntimeError):
    """Raised when Groq research output is missing, malformed, or unsafe to use."""


def _extract_json_object(text: Any) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise GroqAIError("Groq returned empty or non-text content")
    try:
        value = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise GroqAIError("Groq returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise GroqAIError("Groq response must be a JSON object")
    return value


def _validate_review(value: dict[str, Any]) -> dict[str, Any]:
    required_text = ("regime_assessment", "research_only_summary")
    required_arrays = ("key_observations", "data_quality_flags", "strategy_watchlist")
    result: dict[str, Any] = {}

    for key in required_text:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip() or len(item) > 4000:
            raise GroqAIError(f"Groq review field {key!r} must be non-empty text of at most 4000 characters")
        result[key] = item.strip()

    for key in required_arrays:
        items = value.get(key)
        if (
            not isinstance(items, list)
            or len(items) > 20
            or any(not isinstance(item, str) or len(item) > 1000 for item in items)
        ):
            raise GroqAIError(f"Groq review field {key!r} must be an array of at most 20 text values")
        result[key] = [item.strip() for item in items if item.strip()]

    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise GroqAIError("Groq review confidence must be a finite number between 0 and 1")
    if not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0:
        raise GroqAIError("Groq review confidence must be a finite number between 0 and 1")
    result["confidence"] = float(confidence)
    return result


def _research_prompt(research: dict[str, Any], intelligence: dict[str, Any]) -> str:
    external = {
        "news_summary": intelligence.get("news_summary", {}),
        "source_failures": intelligence.get("failures", {}),
    }
    hits = suspicious_instruction_markers(json.dumps(external, default=str))
    if hits:
        _LOGGER.warning("possible prompt-injection markers in external research inputs: %s", hits[:5])
    return (
        "Review this Atlas research packet. Return an object with keys: regime_assessment (text), "
        "key_observations (array of strings), data_quality_flags (array of strings), "
        "strategy_watchlist (array of strings), confidence (number from 0 to 1), and "
        "research_only_summary (text). Do not return buy/sell instructions or order parameters.\n\n"
        + UNTRUSTED_PREAMBLE
        + "\n\n"
        + wrap_untrusted("atlas_computed_research", research)
        + "\n\n"
        + wrap_untrusted("external_news_and_source_status", external)
    )


async def _groq_json(prompt: str) -> dict[str, Any]:
    api_key = str(settings.groq_api_key or "").strip()
    if not api_key:
        raise GroqAIError("Groq API key is not configured")
    model = str(settings.groq_model or "").strip()
    if not model or len(model) > 200:
        raise GroqAIError("Groq model identifier is invalid")
    try:
        max_tokens = int(settings.groq_max_completion_tokens)
        timeout = float(settings.groq_timeout_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GroqAIError("Groq request limits are invalid") from exc
    if not 1 <= max_tokens <= 8192:
        raise GroqAIError("Groq completion-token limit must be between 1 and 8192")
    if not math.isfinite(timeout) or not 1.0 <= timeout <= 60.0:
        raise GroqAIError("Groq timeout must be between 1 and 60 seconds")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_completion_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(_GROQ_CHAT_COMPLETIONS_URL, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
    except asyncio.CancelledError:
        raise
    except httpx.HTTPError as exc:
        raise GroqAIError(f"Groq request failed ({type(exc).__name__})") from exc
    except (TypeError, ValueError) as exc:
        raise GroqAIError("Groq returned an unreadable response body") from exc

    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GroqAIError("Groq response did not contain a chat-completion message") from exc
    return _validate_review(_extract_json_object(text))


async def groq_research_review(
    research: dict[str, Any], intelligence: dict[str, Any]
) -> dict[str, Any]:
    """Return an optional Groq research annotation without granting execution authority."""
    if not settings.groq_research_review_enabled:
        return {
            "status": "DISABLED",
            "provider": "groq",
            "reason": "GROQ_RESEARCH_REVIEW_ENABLED is false",
            "execution_authority": False,
        }
    if not str(settings.groq_api_key or "").strip():
        return {
            "status": "DISABLED",
            "provider": "groq",
            "reason": "Groq API key is not configured",
            "execution_authority": False,
        }
    try:
        review = await _groq_json(_research_prompt(research, intelligence))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOGGER.warning("Groq research review failed (%s)", type(exc).__name__)
        return {
            "status": "ERROR",
            "provider": "groq",
            "error": type(exc).__name__,
            "execution_authority": False,
        }
    return {
        "status": "OK",
        "provider": "groq",
        "model": settings.groq_model,
        "review": review,
        "execution_authority": False,
    }
