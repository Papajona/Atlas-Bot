from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app import main


class _FakeResult:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _FakeDB:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        return _FakeResult(self.row)


def test_research_run_detail_exposes_persisted_groq_review_to_authenticated_operator(monkeypatch):
    groq_review = {
        "status": "OK",
        "provider": "groq",
        "review": {
            "regime_assessment": "Mixed evidence; remain paper-only.",
            "research_only_summary": "Observe candidate with incomplete coverage.",
            "confidence": 0.6,
        },
        "input_chars": 42000,
        "input_summary_chars": 3400,
        "input_truncated": True,
        "execution_authority": False,
    }
    row = type("ResearchRunFixture", (), {
        "id": 17,
        "symbol": "BTC-USD",
        "run_at": datetime(2026, 10, 29, tzinfo=timezone.utc),
        "regime_assessment": "mixed",
        "top_strategy": "TREND",
        "sharpe": 0.7,
        "max_drawdown": -0.1,
        "total_return": 0.04,
        "regime_drift_json": "{}",
        "research_json": json.dumps({"status": "OK", "strategies": []}),
        "review_json": json.dumps({"groq_research_review": groq_review}),
    })()

    async def fake_auth(*_args, **_kwargs):
        return {"role": "OPERATIONS"}

    monkeypatch.setattr(main, "auth", fake_auth)
    monkeypatch.setattr(main, "SessionLocal", lambda: _FakeDB(row))

    result = asyncio.run(main.research_run_detail(17, None, None))
    assert result["groq_research_review"]["status"] == "OK"
    assert result["groq_research_review"]["review"]["research_only_summary"].startswith("Observe")
    assert result["groq_research_review"]["input_truncated"] is True
    assert result["groq_research_review"]["execution_authority"] is False
