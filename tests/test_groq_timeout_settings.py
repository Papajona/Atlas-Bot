from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_enabled_groq_trade_review_accepts_a_positive_native_veto_reserve():
    configured = Settings(
        environment="test",
        groq_trade_review_enabled=True,
        groq_trade_review_timeout_seconds=5.0,
        groq_timeout_seconds=20.0,
        ai_strategy_provider_timeout_seconds=1.5,
        ai_trade_decision_total_timeout_seconds=6.5,
        _env_file=None,
    )

    assert configured.groq_trade_review_enabled is True
    assert configured.ai_strategy_provider_timeout_seconds == 1.5
    assert configured.ai_trade_decision_total_timeout_seconds == 6.5


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"ai_strategy_provider_timeout_seconds": 0.0},
            "AI_STRATEGY_PROVIDER_TIMEOUT_SECONDS must be finite and positive",
        ),
        (
            {"ai_strategy_provider_timeout_seconds": -0.1},
            "AI_STRATEGY_PROVIDER_TIMEOUT_SECONDS must be finite and positive",
        ),
        (
            {"ai_trade_decision_total_timeout_seconds": 0.0},
            "AI_TRADE_DECISION_TOTAL_TIMEOUT_SECONDS must be finite and positive",
        ),
        (
            {"ai_trade_decision_total_timeout_seconds": 1.5},
            "AI_TRADE_DECISION_TOTAL_TIMEOUT_SECONDS must exceed",
        ),
        (
            {"ai_trade_decision_total_timeout_seconds": 1.0},
            "AI_TRADE_DECISION_TOTAL_TIMEOUT_SECONDS must exceed",
        ),
        (
            {"groq_trade_review_timeout_seconds": 0.0},
            "GROQ_TRADE_REVIEW_TIMEOUT_SECONDS must be finite and positive",
        ),
        (
            {"groq_trade_review_timeout_seconds": float("nan")},
            "GROQ_TRADE_REVIEW_TIMEOUT_SECONDS must be finite and positive",
        ),
        (
            {"groq_timeout_seconds": float("inf")},
            "GROQ_TIMEOUT_SECONDS must be finite and positive",
        ),
    ],
)
def test_enabled_groq_trade_review_rejects_invalid_timeout_configuration_at_startup(
    overrides, message
):
    with pytest.raises(ValidationError, match=message):
        Settings(
            environment="test",
            groq_trade_review_enabled=True,
            _env_file=None,
            **overrides,
        )
