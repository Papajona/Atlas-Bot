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
import re
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


_ACTIONABLE_TRADING_PATTERNS = (
    # Keep action verbs case-insensitive but symbol matching case-sensitive for
    # unseparated tickers. This catches BUY BTCUSDT without mistaking ordinary
    # lowercase prose such as "enter risk-off conditions" for a trade command.
    re.compile(
        r"(?i:\b(?:buy|sell|go long|go short|enter|exit|open|close))\s+"
        r"(?:\d+(?:\.\d+)?\s+)?"
        r"(?:[A-Za-z0-9]{2,}(?:[/_][A-Za-z0-9]{2,}){1,2}|"
        r"[A-Z]{3,}[A-Z0-9]*|[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*)"
        r"(?:\s+(?i:now|immediately|at market|market price|at)\b)?"
    ),
    re.compile(
        r"(?i:\b(?:place|submit|execute)\s+(?:a\s+)?"
        r"(?:(?:buy|sell)\s+)?order\b)"
    ),
    re.compile(
        r"(?i:\b(?:entry|stop[- ]loss|take[- ]profit|limit|trigger)"
        r"(?:\s+price)?\s*(?:=|:|at)\s*[$€£]?\d+(?:[.,]\d+)?)"
    ),
    re.compile(r"(?i:\b(?:buy|sell)\s+(?:now|immediately|at market)\b)"),
)

_NEGATED_ACTION_PREFIX = re.compile(
    r"(?i)(?:\bdo\s+not|\bdon['’]?t|\bdont|\bnever|"
    r"\bshould\s+not|\bmust\s+not|\bnot\s+to|\bavoid)"
    r"(?:\s+(?:recommend|instruct|suggest|tell)(?:\s+to)?)?\s*$"
)


def _contains_actionable_trading_instruction(text: str) -> bool:
    """Reject explicit trade commands while retaining clearly negated warnings."""
    for pattern in _ACTIONABLE_TRADING_PATTERNS:
        for match in pattern.finditer(text):
            if not _NEGATED_ACTION_PREFIX.search(text[max(0, match.start() - 80):match.start()]):
                return True
    return False

def _validate_review(value: dict[str, Any]) -> dict[str, Any]:
    required_text = ("regime_assessment", "research_only_summary")
    required_arrays = ("key_observations", "data_quality_flags", "strategy_watchlist")
    result: dict[str, Any] = {}

    for key in required_text:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip() or len(item) > 4000:
            raise GroqAIError(f"Groq review field {key!r} must be non-empty text of at most 4000 characters")
        cleaned = item.strip()
        if _contains_actionable_trading_instruction(cleaned):
            raise GroqAIError(f"Groq review field {key!r} contains actionable trading instructions")
        result[key] = cleaned

    for key in required_arrays:
        items = value.get(key)
        if (
            not isinstance(items, list)
            or len(items) > 20
            or any(not isinstance(item, str) or len(item) > 1000 for item in items)
        ):
            raise GroqAIError(f"Groq review field {key!r} must be an array of at most 20 text values")
        cleaned_items = [item.strip() for item in items if item.strip()]
        if any(_contains_actionable_trading_instruction(item) for item in cleaned_items):
            raise GroqAIError(f"Groq review field {key!r} contains actionable trading instructions")
        result[key] = cleaned_items

    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise GroqAIError("Groq review confidence must be a finite number between 0 and 1")
    if not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0:
        raise GroqAIError("Groq review confidence must be a finite number between 0 and 1")
    result["confidence"] = float(confidence)
    return result


def _bounded_prompt_value(value: Any, state: dict[str, bool], depth: int = 0) -> Any:
    """Compact nested source data so prompt delimiters are never cut mid-value."""
    if depth >= 4:
        state["truncated"] = True
        return "[nested value omitted]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        state["truncated"] = True
        return None
    if isinstance(value, str):
        if len(value) <= 300:
            return value
        state["truncated"] = True
        return value[:300] + f" [omitted {len(value) - 300} characters]"
    if isinstance(value, dict):
        pairs = list(value.items())
        result = {str(key)[:80]: _bounded_prompt_value(item, state, depth + 1)
                  for key, item in pairs[:15]}
        if len(pairs) > 15:
            result["omitted_key_count"] = len(pairs) - 15
            state["truncated"] = True
        return result
    if isinstance(value, (list, tuple)):
        items = list(value)
        result = [_bounded_prompt_value(item, state, depth + 1) for item in items[:12]]
        if len(items) > 12:
            result.append(f"[{len(items) - 12} additional items omitted]")
            state["truncated"] = True
        return result
    return _bounded_prompt_value(str(value), state, depth + 1)


def _research_prompt(
    research: dict[str, Any], intelligence: dict[str, Any]
) -> tuple[str, bool]:
    state = {"truncated": False}
    research = research if isinstance(research, dict) else {}
    validation = research.get("validation") if isinstance(research.get("validation"), dict) else {}
    retained_research_keys = {
        "symbol", "asset", "regime", "research_order", "validation", "data_quality",
        "universe_bias", "paper_candidates", "trial_labels", "prospective_trial_archive",
    }
    if any(key not in retained_research_keys for key in research):
        state["truncated"] = True
    retained_validation_keys = {
        "status", "trial_registry_complete", "walk_forward_purge_complete", "promotion_status",
        "per_strategy_oos",
    }
    if any(key not in retained_validation_keys for key in validation):
        state["truncated"] = True
    oos = validation.get("per_strategy_oos") if isinstance(validation.get("per_strategy_oos"), dict) else {}
    order = research.get("research_order") if isinstance(research.get("research_order"), list) else []
    names = list(dict.fromkeys([str(item) for item in order] + sorted(str(item) for item in oos)))
    selected = names[:40]
    metrics = {}
    for name in selected:
        row = oos.get(name)
        if isinstance(row, dict):
            metric_keys = {"status", "sharpe", "oos_total_return", "max_drawdown", "fold_count", "sample_count"}
            if any(key not in metric_keys for key in row):
                state["truncated"] = True
            metrics[name[:80]] = {key: row[key] for key in
                ("status", "sharpe", "oos_total_return", "max_drawdown", "fold_count", "sample_count")
                if key in row and isinstance(row[key], (str, bool, int, float, type(None)))}
    if len(names) > len(selected):
        state["truncated"] = True
    candidates = research.get("paper_candidates")
    candidates = candidates if isinstance(candidates, list) else []
    candidate_summaries = []
    for candidate in candidates[:15]:
        if isinstance(candidate, dict):
            candidate_summaries.append({key: _bounded_prompt_value(candidate[key], state)
                for key in ("strategy", "status", "score", "confidence", "reason", "data_quality_flags")
                if key in candidate})
        else:
            candidate_summaries.append(_bounded_prompt_value(candidate, state))
    if len(candidates) > 15:
        state["truncated"] = True
    regime = research.get("regime") if isinstance(research.get("regime"), dict) else {}
    quality = research.get("data_quality") if isinstance(research.get("data_quality"), dict) else {}
    bounded_research = {
        "symbol": _bounded_prompt_value(research.get("symbol"), state),
        "asset": _bounded_prompt_value(research.get("asset"), state),
        "regime": _bounded_prompt_value({key: regime[key] for key in
            ("regime", "annualized_realized_vol", "volatility", "status") if key in regime}, state),
        "research_order": selected,
        "strategy_count": len(names),
        "strategy_metrics_omitted": max(0, len(names) - len(selected)),
        "validation": {key: validation[key] for key in
            ("status", "trial_registry_complete", "walk_forward_purge_complete", "promotion_status")
            if key in validation} | {"per_strategy_oos": metrics},
        "data_quality": _bounded_prompt_value(quality, state),
        "universe_bias": _bounded_prompt_value(research.get("universe_bias", quality.get("universe_bias", "UNKNOWN")), state),
        "paper_candidates": candidate_summaries,
        "paper_candidate_count": len(candidates),
        "paper_candidates_omitted": max(0, len(candidates) - len(candidate_summaries)),
        "trial_labels": _bounded_prompt_value(research.get("trial_labels", []), state),
        "prospective_trial_archive": _bounded_prompt_value(research.get("prospective_trial_archive", {}), state),
    }
    external_source = {
        "news_summary": intelligence.get("news_summary", {}),
        "source_failures": intelligence.get("failures", {}),
        "macro_regime": intelligence.get("macro_regime", {}),
    }
    if any(key not in {"news_summary", "failures", "macro_regime"} for key in intelligence):
        state["truncated"] = True
    bounded_external = _bounded_prompt_value(external_source, state)
    research_json = json.dumps(bounded_research, default=str, separators=(",", ":"))
    external_json = json.dumps(bounded_external, default=str, separators=(",", ":"))
    if len(research_json) > 12_000 or len(external_json) > 5_000:
        raise GroqAIError("Compacted Groq research input exceeded its reviewed size bound")
    input_truncated = state["truncated"]
    hits = suspicious_instruction_markers(external_json)
    if hits:
        _LOGGER.warning("possible prompt-injection markers in external research inputs: %s", hits[:5])
    prompt = (
        "Review this Atlas research packet. Return one JSON object with regime_assessment, "
        "key_observations, data_quality_flags, strategy_watchlist, confidence, and research_only_summary. "
        "Do not return buy/sell instructions or order parameters. The supplied values are a bounded structured "
        "summary of computed evidence; omitted rows and fields are disclosed. Do not infer omitted facts.\n\n"
        + UNTRUSTED_PREAMBLE + "\n\n"
        + wrap_untrusted("atlas_computed_research", bounded_research, max_chars=14_000)
        + "\n\n"
        + wrap_untrusted("external_news_and_source_status", bounded_external, max_chars=6_000)
    )
    return prompt, input_truncated

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
    input_truncated: bool | None = None
    try:
        prompt, input_truncated = _research_prompt(research, intelligence)
        review = await _groq_json(prompt)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Keep diagnostics actionable without logging request bodies or credentials.
        cause = exc
        while getattr(cause, "__cause__", None) is not None:
            cause = cause.__cause__
        cause_type = type(cause).__name__
        response = getattr(cause, "response", None)
        raw_status = getattr(response, "status_code", None)
        http_status = raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else None
        _LOGGER.warning(
            "Groq research review failed error_type=%s cause_type=%s http_status=%s",
            type(exc).__name__,
            cause_type,
            http_status,
            exc_info=True,
        )
        return {
            "status": "ERROR",
            "provider": "groq",
            "error": type(exc).__name__,
            "cause": cause_type,
            "http_status": http_status,
            "input_truncated": input_truncated,
            "execution_authority": False,
        }
    return {
        "status": "OK",
        "provider": "groq",
        "model": settings.groq_model,
        "review": review,
        "input_truncated": input_truncated,
        "execution_authority": False,
    }
