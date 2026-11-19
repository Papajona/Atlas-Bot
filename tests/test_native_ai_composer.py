"""Native AI composer regression tests using a labelled local corpus."""
import importlib.util
from pathlib import Path

import pytest

from app.native_ai import NativeAIInputError, compose_strategy_spec

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
