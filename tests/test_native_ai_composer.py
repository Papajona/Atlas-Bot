"""Native AI composer regression tests using a labelled local corpus."""
import importlib.util
from pathlib import Path

import pytest

from app.native_ai import NativeAIInputError, compose_strategy_spec
from app.strategy_ai import AIStrategySpec

ROOT = Path(__file__).resolve().parents[1]
_EVAL_SPEC = importlib.util.spec_from_file_location("eval_native_ai", ROOT / "scripts" / "eval_native_ai.py")
_EVAL = importlib.util.module_from_spec(_EVAL_SPEC)
_EVAL_SPEC.loader.exec_module(_EVAL)


def test_labelled_prompt_corpus_has_no_regressions():
    report = _EVAL.evaluate(ROOT / "app" / "native_ai.py")
    assert report["cases"] >= 20
    assert report["failures"] == {}
    assert report["network_calls"] == 0


@pytest.mark.parametrize(
    "prompt,expected_asset,expected_symbol",
    [
        ("silver trend", "commodity", "XAG_USD"),
        ("Silver / USD momentum", "commodity", "XAG_USD"),
        ("XAGUSD breakout", "commodity", "XAG_USD"),
        ("XAG_USD trend", "commodity", "XAG_USD"),
        ("gold trend", "commodity", "XAU_USD"),
        ("golden cross on BTC", "crypto", "BTC/USDT"),
        ("golden cross on ETH", "crypto", "ETH/USDT"),
    ],
)
def test_native_ai_maps_precious_metals_without_confusing_silver_gold_or_golden_cross(
    prompt, expected_asset, expected_symbol
):
    draft = compose_strategy_spec(prompt=prompt, name="Instrument regression")
    assert draft["asset"] == expected_asset
    assert draft["symbols"] == [expected_symbol]


def test_explicit_silver_symbol_infers_commodity_when_asset_is_unspecified():
    draft = compose_strategy_spec(
        prompt="momentum strategy", name="Silver", symbol="XAG_USD"
    )
    assert draft["asset"] == "commodity"
    assert draft["symbols"] == ["XAG_USD"]


def test_explicit_asset_selection_remains_authoritative():
    draft = compose_strategy_spec(
        prompt="trend strategy", name="Explicit FX", asset="forex"
    )
    assert draft["asset"] == "forex"
    assert draft["symbols"] == ["EUR_USD"]


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("trend BTC on 5m", ["5m"]),
        ("trend BTC on 15 minute chart", ["15m"]),
        ("trend BTC on 30-min candles", ["30m"]),
        ("trend BTC on hourly chart", ["1h"]),
        ("trend BTC on four hour chart", ["4h"]),
        ("trend BTC on the daily timeframe", ["1d"]),
        ("trend BTC on the 2h chart", ["1h"]),
    ],
)
def test_supported_and_unsupported_timeframe_phrasings(prompt, expected):
    draft = compose_strategy_spec(prompt=prompt, name="Timeframe regression")
    assert draft["timeframes"] == expected
    if "2h" in prompt:
        assert any("Unsupported timeframe" in warning for warning in draft["validation_warnings"])


@pytest.mark.parametrize(
    "prompt,stop,target",
    [
        ("stop 1 ATR and take profit 3 ATR, trend BTC 1h", 1.0, 3.0),
        ("take profit 3 ATR and stop 1 ATR, trend BTC 1h", 1.0, 3.0),
        ("trend BTC 1h, 2 ATR stop and 6 ATR target", 2.0, 3.0),
        ("trend BTC 1h, stop loss 2.5 ATR, target 3R", 2.5, 3.0),
        ("trend BTC 1h, target 2.5R", 1.5, 2.5),
    ],
)
def test_atr_and_reward_risk_values_follow_their_labels(prompt, stop, target):
    draft = compose_strategy_spec(prompt=prompt, name="Risk parser regression")
    assert draft["stop_loss_atr"] == pytest.approx(stop)
    assert draft["take_profit_rr"] == pytest.approx(target)


def test_indicator_lookbacks_do_not_become_stop_distances():
    draft = compose_strategy_spec(
        prompt="trend BTC 1h, ATR 14 lookback and 200d moving average",
        name="Indicator periods",
    )
    assert draft["stop_loss_atr"] == pytest.approx(1.5)
    assert not any("Unsupported timeframe" in warning for warning in draft["validation_warnings"])


def test_out_of_range_risk_remains_capped():
    draft = compose_strategy_spec(
        prompt="BTC trend long 1h, risk 2% per trade", name="Risk cap"
    )
    assert draft["risk_per_trade"] <= 0.005
    assert any("capped" in warning.lower() for warning in draft["validation_warnings"])


def test_invalid_explicit_symbol_is_a_client_input_error():
    with pytest.raises(NativeAIInputError):
        compose_strategy_spec(
            prompt="BTC trend", name="Invalid symbol", symbol="<script>"
        )


def _types(conditions):
    return [c["type"] for c in conditions]


def test_unprofiled_prompt_keeps_balanced_defaults():
    draft = compose_strategy_spec(prompt="BTC trend long 1h", name="Balanced")
    assert (draft["risk_per_trade"], draft["stop_loss_atr"], draft["take_profit_rr"]) == (0.005, 1.5, 2.0)
    assert _types(draft["exit_conditions"]) == ["TREND_EXIT"]


@pytest.mark.parametrize("prompt", ["conservative BTC trend long 1h", "low risk BTC trend long 1h", "cautious BTC trend long 1h"])
def test_conservative_profile_halves_risk_widens_stop_and_adds_guards(prompt):
    draft = AIStrategySpec.model_validate(compose_strategy_spec(prompt=prompt, name="Conservative"))
    assert draft.risk_per_trade == pytest.approx(0.0025)
    assert draft.stop_loss_atr == pytest.approx(2.0)
    assert {"HIGHER_TIMEFRAME_AGREEMENT", "CONSERVATIVE_VOLATILITY_CAP"} <= {c.type for c in draft.entry_conditions}
    assert "TIME_STOP" in {c.type for c in draft.exit_conditions}


def test_explicit_numbers_beat_the_conservative_defaults():
    draft = compose_strategy_spec(prompt="cautious BTC trend long 1h, risk 0.4%, stop 3 ATR", name="Explicit")
    assert draft["risk_per_trade"] == pytest.approx(0.004)
    assert draft["stop_loss_atr"] == pytest.approx(3.0)


def test_adaptive_profile_adds_regime_rules_without_changing_risk():
    draft = compose_strategy_spec(prompt="adaptive regime-aware ETH momentum long 4h", name="Adaptive")
    assert {"REGIME_ADAPTATION", "VOLATILITY_SCALED_RISK"} <= set(_types(draft["entry_conditions"]))
    assert draft["risk_per_trade"] == pytest.approx(0.005)
    assert any("Adaptive profile" in w for w in draft["validation_warnings"])


def test_conservative_and_adaptive_combine():
    draft = AIStrategySpec.model_validate(compose_strategy_spec(prompt="conservative adaptive gold mean reversion long daily", name="Both"))
    types = {c.type for c in draft.entry_conditions}
    assert {"HIGHER_TIMEFRAME_AGREEMENT", "REGIME_ADAPTATION"} <= types
    assert draft.risk_per_trade == pytest.approx(0.0025)


@pytest.mark.parametrize("prompt", [
    "not conservative BTC trend long 1h",
    "non-adaptive BTC trend long 1h",
    "non-conservative BTC trend long 1h",
    "BTC trend long 1h without a regime filter",
    "do not use a conservative profile BTC trend long 1h",
    "isn't conservative BTC trend long 1h",
    "isn’t conservative BTC trend long 1h",
    "please don't choose an adaptive profile for ETH momentum long 4h",
    "without using the conservative profile, BTC trend long 1h",
])
def test_negated_profile_words_do_not_activate_a_profile(prompt):
    draft = compose_strategy_spec(prompt=prompt, name="Negated")
    assert draft["risk_per_trade"] == pytest.approx(0.005)
    assert not any("profile" in w.lower() for w in draft["validation_warnings"])
    condition_types = _types(draft["entry_conditions"]) + _types(draft["exit_conditions"])
    assert not ({"HIGHER_TIMEFRAME_AGREEMENT", "CONSERVATIVE_VOLATILITY_CAP", "TIME_STOP",
                 "REGIME_ADAPTATION", "VOLATILITY_SCALED_RISK"} & set(condition_types))


def test_profile_negation_is_clause_local():
    draft = compose_strategy_spec(
        prompt="do not use conservative, but use adaptive BTC trend long 1h",
        name="Clause-local profile",
    )
    assert "REGIME_ADAPTATION" in _types(draft["entry_conditions"])
    assert "HIGHER_TIMEFRAME_AGREEMENT" not in _types(draft["entry_conditions"])


def test_aggressive_wording_never_raises_risk_and_is_reported():
    draft = compose_strategy_spec(prompt="aggressive BTC trend long 1h", name="Aggressive")
    assert draft["risk_per_trade"] <= 0.005
    assert any("Aggressive wording was ignored" in w for w in draft["validation_warnings"])
    both = compose_strategy_spec(prompt="aggressive but conservative BTC trend long 1h", name="Mixed")
    assert both["risk_per_trade"] == pytest.approx(0.0025)


def test_unrecognised_strategy_type_is_flagged_instead_of_silently_trend():
    draft = compose_strategy_spec(prompt="buy BTC on the 1h", name="Fallback")
    assert draft["strategy_family"] == "TREND"
    assert any("No strategy type was recognised" in w for w in draft["validation_warnings"])
    named = compose_strategy_spec(prompt="BTC trend long 1h", name="Named")
    assert not any("No strategy type" in w for w in named["validation_warnings"])


def test_a_timeframe_after_the_word_risk_is_not_a_risk_percentage():
    draft = compose_strategy_spec(prompt="low risk 1h BTC trend long", name="Risk word")
    assert draft["risk_per_trade"] == pytest.approx(0.0025)

@pytest.mark.parametrize(
    "prompt,expected_family",
    [
        ("do not use mean reversion; use breakout BTC long 1h", "BREAKOUT"),
        ("avoid breakout, use momentum BTC long 1h", "MOMENTUM"),
        ("no momentum; use trend following BTC long 1h", "TREND"),
        ("not only momentum but breakout BTC long 1h", "ENSEMBLE"),
    ],
)
def test_negated_strategy_family_does_not_override_affirmative_family(
    prompt, expected_family
):
    draft = compose_strategy_spec(prompt=prompt, name="Negated family")
    assert draft["strategy_family"] == expected_family


def test_only_negated_strategy_families_are_treated_as_unnamed_fallback():
    draft = compose_strategy_spec(
        prompt="do not use mean reversion or momentum for BTC long 1h",
        name="No named family",
    )
    assert draft["strategy_family"] == "TREND"
    assert any(
        "No strategy type was recognised" in warning
        for warning in draft["validation_warnings"]
    )

