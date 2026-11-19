"""Native AI veto/composer hardening. Pure-Python: no network, no app settings."""from datetime import datetime, timezone

from app.native_ai import (
    NativeAIError,
    compose_strategy_spec,
    evaluate_native_trade_packet,
    native_ai_health_check,
)


def _good():
    return {
        "symbol": "BTC/USDT", "asset": "crypto", "exchange": "binance", "timeframe": "1h",
        "market_timestamp": datetime.now(timezone.utc).isoformat(),
        "trade_plan": {"side": "buy", "entry_price": 100.0, "stop_loss_price": 98.0, "take_profit_price": 104.0},
        "deterministic_rules": {"entry": "confirmed"},
        "price_context": {"latest_close": 100.0, "bars": 300, "spread_bps": 2.0, "max_spread_bps": 100.0},
        "data_quality": {"safe": True, "reasons": []},
        "adaptive_model": {"status": "OK"}, "ml_signal": {"signal": 0.4},
    }


def _reasons(mutate):
    packet = _good()
    mutate(packet)
    result = evaluate_native_trade_packet(packet)
    return result["safe"], result["reasons"]


def test_baseline_packet_is_safe():
    assert _reasons(lambda p: None) == (True, ["all_native_safety_checks_passed"])


def test_real_world_timestamp_formats_are_accepted():
    for stamp in ("2026-11-18T10:00:00.123456789+00:00", "2026-11-18 10:00:00+00:00",
                  "2026-11-18T10:00:00", "2026-11-18T10:00:00Z", 1763460000000, 1763460000):
        assert _reasons(lambda p, s=stamp: p.update(market_timestamp=s))[0] is True, stamp


def test_bad_timestamps_are_vetoed():
    assert "invalid_market_timestamp" in _reasons(lambda p: p.update(market_timestamp="banana"))[1]
    assert "market_timestamp_in_future" in _reasons(lambda p: p.update(market_timestamp="2999-01-01T00:00:00Z"))[1]


def test_spread_limit_can_only_be_tightened():
    assert "spread_exceeds_limit" in _reasons(lambda p: p["price_context"].update(spread_bps=150))[1]
    assert "spread_exceeds_limit" in _reasons(lambda p: p["price_context"].update(spread_bps=5000))[1]
    assert "spread_exceeds_limit" in _reasons(lambda p: p["price_context"].update(spread_bps=400, max_spread_bps=10_000))[1]

    def no_limit(p):
        p["price_context"].pop("max_spread_bps")
        p["price_context"]["spread_bps"] = 150

    assert _reasons(no_limit)[0] is True


def test_inconsistent_or_extreme_plans_are_vetoed():
    far = lambda p: p["trade_plan"].update(entry_price=140, stop_loss_price=137, take_profit_price=146)
    assert "entry_price_far_from_latest_close" in _reasons(far)[1]
    wide = lambda p: p["trade_plan"].update(stop_loss_price=40, take_profit_price=190)
    assert "stop_distance_extreme" in _reasons(wide)[1]
    tiny = lambda p: p["trade_plan"].update(stop_loss_price=99.99, take_profit_price=100.02)
    assert "stop_distance_not_above_spread" in _reasons(tiny)[1]


def test_booleans_and_empty_evidence_are_not_accepted():
    assert _reasons(lambda p: p["ml_signal"].update(signal=True))[0] is False
    assert _reasons(lambda p: p["trade_plan"].update(entry_price=True))[0] is False
    assert "deterministic_rules_missing" in _reasons(lambda p: p.update(deterministic_rules={}))[1]


def test_short_trade_still_passes():
    def short(p):
        p["trade_plan"].update(side="sell", entry_price=100, stop_loss_price=102, take_profit_price=96)
        p["ml_signal"].update(signal=-0.3)

    assert _reasons(short)[0] is True


def test_negated_direction_is_not_misread():
    assert compose_strategy_spec(prompt="do not short BTC, buy the dip on 1h", name="n")["direction"] == "LONG"
    assert compose_strategy_spec(prompt="Never go long BTC, short only on 1h", name="n")["direction"] == "SHORT"
    spec = compose_strategy_spec(prompt="never go long EUR/USD, avoid buying", name="n")
    assert any("Negated direction" in w for w in spec["validation_warnings"])
    assert any("No trading direction" in w for w in spec["validation_warnings"])


def test_indicator_period_wording_is_not_a_direction():
    assert compose_strategy_spec(prompt="buy BTC when 20 ema crosses long ema", name="n")["direction"] == "LONG"


def test_symbol_must_be_clean():
    for ok in ("BTC/USDT", "BTC/USDT:USDT", "EUR_USD", "BTC-USD", "BTCUSDT"):
        assert compose_strategy_spec(prompt="BTC trend long", name="n", symbol=ok)["symbols"] == [ok]
    for bad in ("<script>alert(1)</script>", "BTC USDT", "a" * 81, "'; DROP TABLE x;--"):
        try:
            compose_strategy_spec(prompt="BTC trend long", name="n", symbol=bad)
        except NativeAIError:
            continue
        raise AssertionError(f"symbol accepted: {bad!r}")


def test_boolean_risk_cap_falls_back_to_default():
    assert compose_strategy_spec(prompt="BTC trend long", name="n", max_risk_per_trade=True)["risk_per_trade"] == 0.005


def test_health_check_exercises_pass_and_fail_paths():
    report = native_ai_health_check()
    assert report["status"] == "PASS" and report["network_calls"] == 0
    assert all(report["checks"].values())
