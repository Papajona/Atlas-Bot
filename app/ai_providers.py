from __future__ import annotations
import logging

import asyncio
import json
import math
from typing import Any

import httpx

from .config import settings


class AIProviderError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise AIProviderError("AI provider returned empty or non-text output")
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].lstrip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIProviderError(f"AI provider returned non-JSON output: {exc}") from exc
    if not isinstance(value, dict):
        raise AIProviderError("AI provider returned JSON that is not an object")
    return value


async def _gemini_json(prompt: str, *, timeout: float = 30.0, system_instruction: str | None = None, model: str | None = None) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise AIProviderError("GEMINI_API_KEY is not configured")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model or settings.gemini_model}:generateContent"
    payload = {
        "systemInstruction": {
            "parts": [{"text": (
                system_instruction or ("You are Atlas Trading OS's research analyst. Analyze supplied market research only. "
                "Do not place trades, invent market data, or give guaranteed outcomes. Return JSON only.")
            )}]
        },
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    headers = {"x-goog-api-key": settings.gemini_api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError("Gemini response did not contain generated text") from exc
    return _extract_json(text)


async def _cerebras_json(prompt: str, *, timeout: float = 30.0, system_instruction: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Call Cerebras' OpenAI-compatible Chat Completions API; never grants execution authority."""
    if not settings.cerebras_api_key:
        raise AIProviderError("CEREBRAS_API_KEY is not configured")
    url = "https://api.cerebras.ai/v1/chat/completions"
    payload = {
        "model": model or settings.cerebras_model,
        "messages": [
            {"role": "system", "content": (
                system_instruction or ("You are Atlas Trading OS's research assistant. Analyze supplied research only. "
                "Do not place trades, invent market data, or give guaranteed outcomes. Return JSON only.")
            )},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.cerebras_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError("Cerebras response did not contain generated text") from exc
    return _extract_json(text)


async def _groq_json(prompt: str, *, timeout: float = 30.0, system_instruction: str | None = None, model: str | None = None) -> dict[str, Any]:
    if not settings.groq_api_key:
        raise AIProviderError("GROQ_API_KEY is not configured")
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": model or settings.groq_model,
        "messages": [
            {"role": "system", "content": (
                system_instruction or ("You are Atlas Trading OS's fast market-analysis assistant. Analyze supplied research only. "
                "Do not place trades, invent market data, or give guaranteed outcomes. Return JSON only.")
            )},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_completion_tokens": settings.groq_max_completion_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIProviderError("Groq response did not contain generated text") from exc
    return _extract_json(text)


from .prompt_safety import UNTRUSTED_PREAMBLE, wrap_untrusted, suspicious_instruction_markers


def _prompt(research: dict[str, Any], intelligence: dict[str, Any]) -> str:
    external = {"news_summary": intelligence.get("news_summary", {}), "source_failures": intelligence.get("failures", {})}
    hits = suspicious_instruction_markers(json.dumps(external, default=str))
    if hits:
        logging.getLogger(__name__).warning("possible prompt-injection markers in external research inputs: %s", hits[:5])
    return (
        "Review this Atlas research packet. Return an object with keys: "
        "regime_assessment, key_observations (array), data_quality_flags (array), "
        "strategy_watchlist (array), confidence (0..1), and research_only_summary. "
        "Do not output buy/sell instructions.\n\n" + UNTRUSTED_PREAMBLE + "\n\n"
        "Atlas computed research (trusted):\n" + json.dumps(research, default=str) + "\n\n"
        + wrap_untrusted("external_news_and_source_status", external)
    )


def _validate_research_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the fields declared by the research-review prompt before returning provider output."""
    if not isinstance(value, dict):
        raise AIProviderError("Research-review response must be a JSON object")
    for field in ("regime_assessment", "research_only_summary"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise AIProviderError(f"Research-review response requires non-empty string {field}")
    for field in ("key_observations", "data_quality_flags", "strategy_watchlist"):
        items = value.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise AIProviderError(f"Research-review response requires {field} to be an array of strings")
    confidence = value.get("confidence")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0):
        raise AIProviderError("Research-review response requires finite confidence between 0 and 1")
    return value


async def _validated_research_call(call) -> dict[str, Any]:
    return _validate_research_payload(await call)


async def dual_ai_research_review(research: dict[str, Any], intelligence: dict[str, Any]) -> dict[str, Any]:
    """Run configured research providers independently and return auditable outputs.

    Gemini and Groq remain the established providers; Cerebras is an optional third
    research opinion. None of these outputs has authority to execute orders.
    """
    prompt = _prompt(research, intelligence)
    calls = [("gemini", _validated_research_call(_gemini_json(prompt))), ("groq", _validated_research_call(_groq_json(prompt)))]
    if settings.cerebras_api_key:
        calls.append(("cerebras", _validated_research_call(_cerebras_json(prompt))))
    results = await asyncio.gather(*(call for _, call in calls), return_exceptions=True)
    providers: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for (name, _), result in zip(calls, results, strict=True):
        if isinstance(result, Exception):
            errors[name] = str(result)
        else:
            providers[name] = result
    return {
        "mode": "MULTI_PROVIDER_AI_RESEARCH_REVIEW",
        "providers": providers,
        "errors": errors,
        "execution_authority": False,
    }


def _validate_trade_safety_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Reject semantically incomplete model output before it can participate in a safety vote."""
    if not isinstance(value, dict):
        raise AIProviderError("Trade-safety response must be a JSON object")
    if type(value.get("safe")) is not bool:
        raise AIProviderError("Trade-safety response requires a boolean safe field")
    confidence = value.get("confidence")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0):
        raise AIProviderError("Trade-safety response requires finite confidence between 0 and 1")
    for field in ("reasons", "data_quality_flags"):
        items = value.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise AIProviderError(f"Trade-safety response requires {field} to be an array of strings")
    return value


async def _validated_trade_safety_call(call) -> dict[str, Any]:
    return _validate_trade_safety_payload(await call)


async def dual_ai_trade_safety_review(trade_packet: dict[str, Any]) -> dict[str, Any]:
    """Dual-provider safety veto for a deterministic trade plan.

    Gemini is preferred for the primary opinion; Cerebras may replace it if Gemini
    fails. Groq must independently approve. Providers cannot change trade parameters,
    and any missing or failed required opinion is unsafe.
    """
    prompt = (
        "Review this proposed Atlas trade plan for safety only. Return JSON with keys "
        "safe (boolean), confidence (0..1), reasons (array), and data_quality_flags (array). "
        "Do not change or invent entry, stop, target, side, quantity, or prices. Reject if "
        "the supplied plan is internally inconsistent, lacks risk controls, or market/data "
        "quality is insufficient. This is a veto check, not a trading instruction.\n\n"
        + UNTRUSTED_PREAMBLE + "\n\n" + wrap_untrusted("trade_plan_under_review", trade_packet)
    )
    # Preserve the two-provider safety requirement: Gemini is preferred, Cerebras
    # may substitute only when Gemini is unavailable, and Groq must independently pass.
    if not settings.gemini_api_key and settings.cerebras_api_key:
        primary_name = "cerebras"
        primary_call = _validated_trade_safety_call(_cerebras_json(prompt))
    else:
        primary_name = "gemini"
        primary_call = _validated_trade_safety_call(_gemini_json(prompt))
    primary_result, groq_result = await asyncio.gather(
        primary_call, _validated_trade_safety_call(_groq_json(prompt)), return_exceptions=True
    )
    fallback_used = False
    if isinstance(primary_result, Exception) and primary_name == "gemini" and settings.cerebras_api_key:
        try:
            primary_result = await _validated_trade_safety_call(_cerebras_json(prompt))
            primary_name = "cerebras"
            fallback_used = True
        except Exception as exc:
            primary_result = exc
    providers: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, result in ((primary_name, primary_result), ("groq", groq_result)):
        if isinstance(result, Exception):
            errors[name] = str(result)
        else:
            providers[name] = result
    decisions = []
    required_names = (primary_name, "groq")
    for name in required_names:
        value = providers.get(name)
        decisions.append(bool(value and value.get("safe") is True))
    configured = bool((settings.gemini_api_key or settings.cerebras_api_key) and settings.groq_api_key)
    return {
        "mode": "DUAL_PROVIDER_TRADE_SAFETY_VETO",
        "safe": bool(len(decisions) == 2 and all(decisions) and not errors),
        "configured": configured,
        "providers": providers,
        "errors": errors,
        "fallbacks": {"gemini": "cerebras"} if fallback_used else {},
        "execution_authority": False,
    }
