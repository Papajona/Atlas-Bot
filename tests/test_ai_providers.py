import asyncio


def test_native_safety_review_fails_closed_for_incomplete_packet():
    from app.ai_providers import dual_ai_trade_safety_review
    result = asyncio.run(dual_ai_trade_safety_review({"symbol": "BTC/USDT"}))
    assert result["safe"] is False
    assert result["configured"] is True
    assert result["mode"] == "ATLAS_NATIVE_AI_SAFETY_VETO"
    assert result["execution_authority"] is False
    assert result["providers"]["atlas_native"]["safe"] is False


def test_native_safety_review_accepts_only_complete_consistent_packet():
    from app.native_ai import evaluate_native_trade_packet
    packet = {
        "symbol":"BTC/USDT", "asset":"crypto", "exchange":"paper", "timeframe":"1h",
        "market_timestamp":"2026-10-27T00:00:00+00:00",
        "trade_plan":{"side":"buy","entry_price":100.0,"stop_loss_price":95.0,"take_profit_price":110.0},
        "deterministic_rules":{"entry":"confirmed"},
        "price_context":{"latest_close":100.0,"bars":1000,"spread_bps":2.0},
        "data_quality":{"safe":True,"reasons":[]},
        "adaptive_model":{"status":"PROMOTED","model_version":"fixture"},
        "ml_signal":{"signal":1.0},
    }
    result = evaluate_native_trade_packet(packet)
    assert result["safe"] is True
    assert result["execution_authority"] is False


def test_native_safety_veto_blocks_bad_data_or_invalid_long_prices():
    from app.native_ai import evaluate_native_trade_packet
    packet = {
        "symbol":"BTC/USDT","asset":"crypto","exchange":"paper","timeframe":"1h","market_timestamp":"now",
        "trade_plan":{"side":"buy","entry_price":100,"stop_loss_price":105,"take_profit_price":110},
        "deterministic_rules":{},"price_context":{"latest_close":100,"bars":1000,"spread_bps":2},
        "data_quality":{"safe":False,"reasons":["stale"]},"adaptive_model":{"status":"PROMOTED"},
        "ml_signal":{"signal":1},
    }
    result = evaluate_native_trade_packet(packet)
    assert result["safe"] is False
    assert "market_data_quality_not_safe" in result["reasons"]
    assert "long_stop_entry_target_order_invalid" in result["reasons"]


def test_native_research_review_reports_gaps_and_does_not_authorize_actions():
    from app.ai_providers import dual_ai_research_review
    result = asyncio.run(dual_ai_research_review({}, {"failures":{"news":"unavailable"}}))
    review = result["providers"]["atlas_native"]
    assert result["mode"] == "ATLAS_NATIVE_RESEARCH_REVIEW"
    assert result["network_calls"] == 0
    assert result["execution_authority"] is False
    assert review["execution_authority"] is False
    assert "REGIME_NOT_AVAILABLE" in review["data_quality_flags"]
    assert "TRIAL_REGISTRY_NOT_VERIFIED" in review["data_quality_flags"]


def test_native_ai_self_check_is_offline():
    from app.native_ai import native_ai_health_check
    report = native_ai_health_check()
    assert report["status"] == "PASS"
    assert report["network_calls"] == 0
    assert report["external_model_credentials_required"] is False



def test_native_safety_allows_only_explicitly_disabled_adaptive_mode_with_other_gates_intact():
    from app.native_ai import evaluate_native_trade_packet

    packet = {
        "symbol": "BTC/USDT", "asset": "crypto", "exchange": "paper", "timeframe": "1h",
        "market_timestamp": "2026-10-27T00:00:00+00:00",
        "trade_plan": {"side": "buy", "entry_price": 100.0, "stop_loss_price": 95.0, "take_profit_price": 110.0},
        "deterministic_rules": {"entry": "confirmed"},
        "price_context": {"latest_close": 100.0, "bars": 1000, "spread_bps": 2.0},
        "data_quality": {"safe": True, "reasons": []},
        "adaptive_model": {"status": "DISABLED"},
        "ml_signal": {"status": "DISABLED"},
    }
    result = evaluate_native_trade_packet(packet)
    assert result["safe"] is True
    assert "ADAPTIVE_AI_DISABLED" in result["data_quality_flags"]
    assert result["execution_authority"] is False

    packet["ml_signal"] = {"status": "DISABLED", "signal": 0}
    result = evaluate_native_trade_packet(packet)
    assert result["safe"] is False
    assert "ml_signal_is_not_directional" in result["reasons"]


def _valid_groq_required_packet(review=None, *, input_truncated=False, status="OK"):
    packet = {
        "symbol": "BTC/USDT",
        "asset": "crypto",
        "exchange": "paper",
        "timeframe": "1h",
        "market_timestamp": "2026-10-30T10:00:00+00:00",
        "trade_plan": {
            "side": "buy",
            "entry_price": 100.0,
            "stop_loss_price": 95.0,
            "take_profit_price": 110.0,
        },
        "deterministic_rules": {"entry": "confirmed"},
        "price_context": {"latest_close": 100.0, "bars": 500, "spread_bps": 2.0},
        "data_quality": {"safe": True, "reasons": []},
        "adaptive_model": {"status": "PROMOTED", "model_version": "paper-fixture"},
        "ml_signal": {"signal": 1.0},
        "groq_trade_review_required": True,
        "groq_trade_review": {
            "status": status,
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "input_truncated": input_truncated,
            "execution_authority": False,
            "review": review or {
                "regime_assessment": "Mixed but usable market context.",
                "setup_alignment": "NEUTRAL",
                "risk_level": "MODERATE",
                "confidence": 0.86,
                "key_observations": ["No high-confidence contradiction in the supplied evidence."],
                "contradictions": [],
                "data_quality_flags": [],
            },
        },
    }
    return packet


def test_native_ai_requires_a_successful_complete_groq_review_when_enabled():
    from app.native_ai import evaluate_native_trade_packet

    packet = _valid_groq_required_packet()
    result = evaluate_native_trade_packet(packet)
    assert result["safe"] is True
    assert "GROQ_TRADE_REVIEW_INCLUDED" in result["data_quality_flags"]

    packet["groq_trade_review"]["input_truncated"] = True
    truncated = evaluate_native_trade_packet(packet)
    assert truncated["safe"] is False
    assert "groq_trade_review_input_incomplete" in truncated["reasons"]

    packet["groq_trade_review"] = {"status": "ERROR", "provider": "groq", "execution_authority": False}
    unavailable = evaluate_native_trade_packet(packet)
    assert unavailable["safe"] is False
    assert "groq_trade_review_unavailable" in unavailable["reasons"]


def test_native_ai_vetoes_high_confidence_groq_contradiction_and_critical_risk():
    from app.native_ai import evaluate_native_trade_packet

    contradictory = _valid_groq_required_packet({
        "regime_assessment": "The submitted technical context conflicts with the regime evidence.",
        "setup_alignment": "CONTRADICTS",
        "risk_level": "MODERATE",
        "confidence": 0.90,
        "key_observations": ["Independent inputs disagree."],
        "contradictions": ["The strategy's stated regime is not supported."],
        "data_quality_flags": [],
    })
    result = evaluate_native_trade_packet(contradictory)
    assert result["safe"] is False
    assert "groq_setup_contradiction_veto" in result["reasons"]

    critical = _valid_groq_required_packet({
        "regime_assessment": "The supplied market context indicates critical risk.",
        "setup_alignment": "NEUTRAL",
        "risk_level": "CRITICAL",
        "confidence": 0.79,
        "key_observations": ["Risk regime is critical."],
        "contradictions": [],
        "data_quality_flags": [],
    })
    critical_result = evaluate_native_trade_packet(critical)
    assert critical_result["safe"] is False
    assert "groq_critical_risk_veto" in critical_result["reasons"]

    high = _valid_groq_required_packet({
        "regime_assessment": "The supplied market context indicates high risk.",
        "setup_alignment": "NEUTRAL",
        "risk_level": "HIGH",
        "confidence": 0.85,
        "key_observations": ["Risk regime is high."],
        "contradictions": [],
        "data_quality_flags": [],
    })
    high_result = evaluate_native_trade_packet(high)
    assert high_result["safe"] is False
    assert "groq_high_risk_veto" in high_result["reasons"]


def test_native_safety_audit_exposes_groq_as_advisory_not_execution_authority():
    from app.native_ai import native_trade_safety_review

    packet = _valid_groq_required_packet()
    result = asyncio.run(native_trade_safety_review(packet))
    assert result["providers"]["groq"]["status"] == "OK"
    assert result["providers"]["groq"]["execution_authority"] is False
    assert result["providers"]["atlas_native"]["execution_authority"] is False
