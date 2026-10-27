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
