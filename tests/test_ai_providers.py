import asyncio


def test_dual_ai_returns_both_provider_outputs(monkeypatch):
    import app.ai_providers as ap
    monkeypatch.setattr(ap.settings, "cerebras_api_key", "")

    async def fake_gemini(prompt):
        return {"provider": "gemini", "confidence": 0.8}

    async def fake_groq(prompt):
        return {"provider": "groq", "confidence": 0.7}

    monkeypatch.setattr(ap, "_gemini_json", fake_gemini)
    monkeypatch.setattr(ap, "_groq_json", fake_groq)
    out = asyncio.run(ap.dual_ai_research_review({"regime": {}}, {"news_summary": {}}))
    assert out["mode"] == "MULTI_PROVIDER_AI_RESEARCH_REVIEW"
    assert set(out["providers"]) == {"gemini", "groq"}
    assert out["execution_authority"] is False


def test_dual_ai_degrades_per_provider(monkeypatch):
    import app.ai_providers as ap

    async def fake_gemini(prompt):
        raise ap.AIProviderError("Gemini unavailable")

    async def fake_groq(prompt):
        return {"provider": "groq"}

    monkeypatch.setattr(ap, "_gemini_json", fake_gemini)
    monkeypatch.setattr(ap, "_groq_json", fake_groq)
    monkeypatch.setattr(ap.settings, "cerebras_api_key", "")
    out = asyncio.run(ap.dual_ai_research_review({}, {}))
    assert "gemini" in out["errors"]
    assert out["providers"]["groq"]["provider"] == "groq"
    assert out["execution_authority"] is False


def test_config_exposes_configured_ai_provider_models():
    from app.config import settings
    assert settings.gemini_model
    assert settings.groq_model
    assert settings.cerebras_model
    assert not hasattr(settings, "claude_api_key")


def test_trade_safety_review_flags_unconfigured_distinctly_from_vetoed(monkeypatch):
    # An unconfigured AI Brain (no provider keys set) must still fail closed (safe=False,
    # no trades), but callers need to tell "nothing is configured" apart from "the AI
    # reviewed a real signal and rejected it" so an empty deployment doesn't get
    # misdiagnosed as a broken strategy.
    import app.ai_providers as ap

    async def fake_unconfigured(prompt):
        raise ap.AIProviderError("not configured")

    monkeypatch.setattr(ap, "_gemini_json", fake_unconfigured)
    monkeypatch.setattr(ap, "_groq_json", fake_unconfigured)
    monkeypatch.setattr(ap.settings, "gemini_api_key", "")
    monkeypatch.setattr(ap.settings, "groq_api_key", "")
    monkeypatch.setattr(ap.settings, "cerebras_api_key", "")
    out = asyncio.run(ap.dual_ai_trade_safety_review({"symbol": "BTCUSDT"}))
    assert out["safe"] is False
    assert out["configured"] is False

    async def fake_vetoed(prompt):
        return {"safe": False, "reasons": ["risk too high"]}

    monkeypatch.setattr(ap, "_gemini_json", fake_vetoed)
    monkeypatch.setattr(ap, "_groq_json", fake_vetoed)
    monkeypatch.setattr(ap.settings, "gemini_api_key", "configured-key")
    monkeypatch.setattr(ap.settings, "groq_api_key", "configured-groq")
    out2 = asyncio.run(ap.dual_ai_trade_safety_review({"symbol": "BTCUSDT"}))
    assert out2["safe"] is False
    assert out2["configured"] is True

def test_cerebras_can_substitute_for_exhausted_gemini_but_groq_remains_required(monkeypatch):
    import app.ai_providers as ap

    monkeypatch.setattr(ap.settings, "gemini_api_key", "configured-gemini")
    monkeypatch.setattr(ap.settings, "groq_api_key", "configured-groq")
    monkeypatch.setattr(ap.settings, "cerebras_api_key", "configured-cerebras")

    async def fail_gemini(prompt):
        raise ap.AIProviderError("Gemini quota exhausted")

    async def safe_cerebras(prompt):
        return {"safe": True, "provider": "cerebras"}

    async def safe_groq(prompt):
        return {"safe": True, "provider": "groq"}

    monkeypatch.setattr(ap, "_gemini_json", fail_gemini)
    monkeypatch.setattr(ap, "_cerebras_json", safe_cerebras)
    monkeypatch.setattr(ap, "_groq_json", safe_groq)
    out = asyncio.run(ap.dual_ai_trade_safety_review({"symbol": "BTCUSDT"}))
    assert out["safe"] is True
    assert set(out["providers"]) == {"cerebras", "groq"}
    assert "gemini" not in out["providers"]
    assert out["execution_authority"] is False
    assert out["fallbacks"] == {"gemini": "cerebras"}

    async def fail_groq(prompt):
        raise ap.AIProviderError("Groq unavailable")

    monkeypatch.setattr(ap, "_groq_json", fail_groq)
    out2 = asyncio.run(ap.dual_ai_trade_safety_review({"symbol": "BTCUSDT"}))
    assert out2["safe"] is False
    assert out2["execution_authority"] is False
