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


_ACTIONABLE_TRADE_INSTRUCTION_RE = re.compile(
    r"(?i)\\b(?:"
    r"(?:buy|sell)\\s+(?:now|immediately|[A-Z0-9][A-Z0-9./:_-]{0,30}\\s+(?:now|immediately|at\\s+(?:\\$?\\d)))|"
    r"go\\s+(?:long|short)|"
    r"open\\s+(?:a\\s+)?(?:long|short)\\s+(?:position|trade)|"
    r"enter\\s+(?:a\\s+)?(?:long|short)\\s+(?:position|trade)|"
    r"place\\s+(?:an?\\s+)?order|execute\\s+(?:a\\s+)?trade|"
    r"set\\s+(?:the\\s+)?(?:stop[- ]loss|take[- ]profit|entry|target)"
    r")\\b"
)


class GroqAIError(RuntimeError):
    """A safe, classifiable Groq failure that never includes credentials or response bodies."""

    def __init__(self, message: str, *, category: str = "VALIDATION", status_code: int | None = None):
        super().__init__(message)
        self.category = category
        self.status_code = status_code


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
    rendered_text = [result[key] for key in required_text] + [
        item for key in required_arrays for item in result[key]
    ]
    if any(_ACTIONABLE_TRADE_INSTRUCTION_RE.search(item) for item in rendered_text):
        raise GroqAIError("Groq review contains actionable trading instructions")
    return result


def _compact_scalar(value: Any, *, max_chars: int = 180) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "…"
    return str(value)[:max_chars]


def _pick_fields(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for field in fields:
        if field not in value:
            continue
        item = value[field]
        if isinstance(item, dict):
            result[field] = _pick_fields(
                item,
                ("status", "valid", "score", "value", "sharpe", "oos_sharpe",
                 "oos_total_return", "total_return", "max_drawdown", "trade_count",
                 "win_rate", "profit_factor", "cost_stress_status"),
            )
        elif isinstance(item, list):
            result[field] = [_compact_scalar(part, max_chars=140) for part in item[:12]]
        else:
            result[field] = _compact_scalar(item)
    return result


def _compact_rows(value: Any, fields: tuple[str, ...], *, limit: int = 6) -> list[Any]:
    if not isinstance(value, list):
        return []
    rows: list[Any] = []
    for item in value[:limit]:
        rows.append(_pick_fields(item, fields) if isinstance(item, dict) else _compact_scalar(item))
    return rows


def _bounded_research_summary(research: dict[str, Any], candidates: Any) -> tuple[dict[str, Any], int, int, bool]:
    """Keep decision-relevant evidence/candidates without slicing JSON mid-object."""
    source = {"research": research, "paper_candidates": candidates}
    source_chars = len(json.dumps(source, default=str, separators=(",", ":")))
    data_quality = research.get("data_quality")
    compact_quality = _pick_fields(
        data_quality,
        ("status", "valid", "issues", "warnings", "coverage", "missing_bars", "source", "universe_bias"),
    )
    if isinstance(data_quality, dict):
        for key in ("issues", "warnings"):
            if isinstance(data_quality.get(key), list):
                compact_quality[key] = [_compact_scalar(item, max_chars=160) for item in data_quality[key][:10]]

    regime = _pick_fields(
        research.get("regime"),
        ("regime", "status", "annualized_realized_vol", "volatility_state", "trend_state"),
    )
    validation = _pick_fields(
        research.get("validation"),
        ("status", "trial_labels", "adjusted_sharpe", "deflated_sharpe", "oos_status",
         "holdout_status", "promotion_status", "fold_count"),
    )
    if isinstance(research.get("validation"), dict):
        per_strategy = research["validation"].get("per_strategy_oos")
        if isinstance(per_strategy, dict):
            validation["per_strategy_oos"] = {
                str(name)[:80]: _pick_fields(
                    row,
                    ("status", "oos_total_return", "sharpe", "max_drawdown", "trade_count",
                     "cost_stress_status", "trade_boundary_overlap_bars"),
                )
                for name, row in list(per_strategy.items())[:6] if isinstance(row, dict)
            }

    candidate_fields = (
        "symbol", "strategy", "name", "status", "eligibility", "promotion_status",
        "score", "reason", "blockers", "oos_status", "sharpe", "oos_total_return",
        "max_drawdown", "cost_stress_status",
    )
    strategy_fields = (
        "strategy", "name", "status", "sharpe", "total_return", "oos_total_return",
        "max_drawdown", "win_rate", "profit_factor", "trade_count", "oos_status",
        "cost_stress_status", "evidence_status",
    )
    summary: dict[str, Any] = {
        "symbol": _compact_scalar(research.get("symbol")),
        "status": _compact_scalar(research.get("status")),
        "asset_class": _compact_scalar(research.get("asset_class")),
        "regime": regime,
        "universe_bias": _compact_scalar(research.get("universe_bias")),
        "data_quality": compact_quality,
        "research_order": _compact_rows(research.get("research_order"), (), limit=8),
        "strategy_count": _compact_scalar(research.get("strategy_count")),
        "strategies": _compact_rows(research.get("strategies"), strategy_fields, limit=6),
        "validation": validation,
        "cost_analysis": _pick_fields(
            research.get("cost_analysis") or research.get("cost_sensitivity"),
            ("status", "cost_stress_status", "cost_stress_multiplier", "baseline_cost_bps",
             "stressed_cost_bps", "net_return", "sharpe", "data_source", "evidence_id"),
        ),
        "prospective_trial_archive": _pick_fields(
            research.get("prospective_trial_archive"),
            ("status", "inserted", "unverified_records", "promotion_evidence_complete"),
        ),
        "paper_candidates": _compact_rows(candidates, candidate_fields, limit=6),
    }
    summary_chars = len(json.dumps(summary, default=str, separators=(",", ":")))
    while summary_chars > 12_000:
        if summary["paper_candidates"]:
            summary["paper_candidates"].pop()
        elif summary["strategies"]:
            summary["strategies"].pop()
        elif validation.get("per_strategy_oos"):
            validation["per_strategy_oos"].pop(next(reversed(validation["per_strategy_oos"])))
        elif compact_quality.get("issues"):
            compact_quality["issues"].pop()
        else:
            summary = {"symbol": summary.get("symbol"), "status": summary.get("status"),
                       "asset_class": summary.get("asset_class"), "regime": summary.get("regime"),
                       "universe_bias": summary.get("universe_bias"),
                       "paper_candidates": summary.get("paper_candidates", [])[:2]}
        summary_chars = len(json.dumps(summary, default=str, separators=(",", ":")))
    return summary, source_chars, summary_chars, True


def _research_prompt_details(
    research: dict[str, Any], intelligence: dict[str, Any], paper_candidates: Any = None
) -> tuple[str, dict[str, Any]]:
    candidates = paper_candidates if paper_candidates is not None else research.get("paper_candidates", [])
    summary, input_chars, summary_chars, input_truncated = _bounded_research_summary(research, candidates)
    failures = intelligence.get("failures")
    failure_keys = tuple(list(failures.keys())[:12]) if isinstance(failures, dict) else ()
    external = {
        "news_summary": _pick_fields(intelligence.get("news_summary"),
            ("article_count", "average_sentiment", "top_topics", "sentiment", "status", "source")),
        "source_failures": _pick_fields(failures, failure_keys),
    }
    news_summary = intelligence.get("news_summary")
    if isinstance(news_summary, str):
        external["news_summary"] = _compact_scalar(news_summary, max_chars=1200)
    hits = suspicious_instruction_markers(json.dumps(external, default=str))
    if hits:
        _LOGGER.warning("possible prompt-injection markers in external research inputs: %s", hits[:5])
    prompt = (
        "Review this Atlas research evidence summary. The packet may be summarized; use only the fields shown and do not infer omitted metrics. "
        "Return one JSON object with keys: regime_assessment (text), key_observations (array of strings), data_quality_flags "
        "(array of strings), strategy_watchlist (array of strings), confidence (number from 0 to 1), and research_only_summary (text). "
        "Do not return buy/sell instructions or order parameters.\n\n"
        + UNTRUSTED_PREAMBLE + "\n\n"
        + wrap_untrusted("atlas_computed_research", summary, max_chars=12_500) + "\n\n"
        + wrap_untrusted("external_news_and_source_status", external, max_chars=3_000)
    )
    return prompt, {"input_chars": input_chars, "input_summary_chars": summary_chars, "input_truncated": input_truncated}


def _research_prompt(research: dict[str, Any], intelligence: dict[str, Any], paper_candidates: Any = None) -> str:
    """Build a bounded, valid-JSON evidence summary for the optional reviewer."""
    return _research_prompt_details(research, intelligence, paper_candidates)[0]

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
    except httpx.HTTPStatusError as exc:
        status_code = int(exc.response.status_code)
        category = ("AUTHENTICATION" if status_code in {401, 403} else
                    "RATE_LIMIT" if status_code == 429 else
                    "UPSTREAM_SERVER" if status_code >= 500 else "UPSTREAM_HTTP")
        raise GroqAIError(f"Groq upstream returned HTTP {status_code}",
                          category=category, status_code=status_code) from exc
    except httpx.TimeoutException as exc:
        raise GroqAIError("Groq request timed out", category="TIMEOUT") from exc
    except httpx.RequestError as exc:
        raise GroqAIError("Groq network request failed", category="NETWORK") from exc
    except httpx.HTTPError as exc:
        raise GroqAIError("Groq HTTP request failed", category="HTTP_ERROR") from exc
    except (TypeError, ValueError) as exc:
        raise GroqAIError("Groq returned an unreadable response body", category="RESPONSE") from exc

    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GroqAIError("Groq response did not contain a chat-completion message") from exc
    return _validate_review(_extract_json_object(text))


async def groq_research_review(
    research: dict[str, Any], intelligence: dict[str, Any], paper_candidates: Any = None
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
    metadata = {"input_chars": 0, "input_summary_chars": 0, "input_truncated": False}
    try:
        prompt, metadata = _research_prompt_details(research, intelligence, paper_candidates)
        review = await _groq_json(prompt)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        category = getattr(exc, "category", "UNEXPECTED")
        status_code = getattr(exc, "status_code", None)
        _LOGGER.warning(
            "Groq research review failed category=%s status_code=%s error_type=%s",
            category, status_code, type(exc).__name__, exc_info=True,
        )
        result = {
            "status": "ERROR", "provider": "groq", "error": type(exc).__name__,
            "error_detail": str(exc)[:240] if isinstance(exc, GroqAIError) else "Unexpected review failure",
            "failure_category": category, "execution_authority": False, **metadata,
        }
        if isinstance(status_code, int):
            result["status_code"] = status_code
        return result
    return {
        "status": "OK", "provider": "groq", "model": settings.groq_model,
        "review": review, "execution_authority": False, **metadata,
    }
