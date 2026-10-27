"""Compatibility exports and pure validators for Atlas Native AI.

No hosted model adapters, provider endpoints, credentials, retry loop or HTTP client lives here.
The parsing helpers remain for existing consumers/tests but do not perform any network requests.
"""
from __future__ import annotations

import json
import math
from typing import Any

from .native_ai import (
    AIProviderError,
    NativeAIError,
    dual_ai_research_review,
    dual_ai_trade_safety_review,
    evaluate_native_trade_packet,
    native_ai_health_check,
    native_research_review,
    native_trade_safety_review,
)


def _response_json_object(response: Any, provider: str = "local component") -> dict[str, Any]:
    """Normalize malformed/non-object JSON bodies to a typed local error; no request is made."""
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError, TypeError, AttributeError) as exc:
        raise AIProviderError(f"{provider} returned an invalid JSON response body") from exc
    if not isinstance(body, dict):
        raise AIProviderError(f"{provider} response body must be a JSON object")
    return body


def _extract_json(text: Any) -> dict[str, Any]:
    """Parse a JSON object from text, retaining the typed fail-closed legacy contract."""
    if not isinstance(text, str) or not text.strip():
        raise AIProviderError("Atlas Native AI received empty or non-text JSON input")
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].lstrip()
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AIProviderError(f"Atlas Native AI received invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AIProviderError("Atlas Native AI requires a JSON object")
    return value


def _validate_trade_safety_payload(value: Any) -> dict[str, Any]:
    """Validate the old typed safety-result contract for local callers; no inference is involved."""
    if not isinstance(value, dict):
        raise AIProviderError("Trade-safety result must be a JSON object")
    if type(value.get("safe")) is not bool:
        raise AIProviderError("Trade-safety result requires a boolean safe field")
    confidence = value.get("confidence")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0):
        raise AIProviderError("Trade-safety result requires finite confidence between 0 and 1")
    for field in ("reasons", "data_quality_flags"):
        items = value.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise AIProviderError(f"Trade-safety result requires {field} to be an array of strings")
    if value["safe"] is True and not value["reasons"]:
        raise AIProviderError("Trade-safety approval requires at least one concrete reason")
    return value


__all__ = [
    "AIProviderError", "NativeAIError", "dual_ai_research_review", "dual_ai_trade_safety_review",
    "evaluate_native_trade_packet", "native_ai_health_check", "native_research_review", "native_trade_safety_review",
    "_extract_json", "_response_json_object", "_validate_trade_safety_payload",
]
