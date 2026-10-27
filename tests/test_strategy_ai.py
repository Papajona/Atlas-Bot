import asyncio
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_native_strategy_composer_builds_validated_paper_only_draft():
    from app.strategy_ai import generate_strategy_draft
    result = asyncio.run(generate_strategy_draft(
        prompt="BTC breakout strategy on 1h, risk 0.5%, target 2R",
        name="BTC local strategy", asset="crypto", symbol="BTC/USDT", timeframe="1h"))
    spec = result["strategy"]
    assert spec["strategy_family"] == "BREAKOUT"
    assert spec["symbols"] == ["BTC/USDT"]
    assert spec["timeframes"] == ["1h"]
    assert spec["risk_per_trade"] <= 0.005
    assert spec["execution_mode"] == "PAPER_SHADOW_ONLY"
    assert spec["requires_backtest"] and spec["requires_walk_forward"]
    assert result["ai"]["provider"] == "atlas_native"
    assert result["ai"]["network_calls"] == 0
    assert result["ai"]["execution_authority"] is False


def test_native_strategy_parser_caps_risk_and_rejects_empty_prompt():
    from app.native_ai import NativeAIError, compose_strategy_spec
    with pytest.raises(NativeAIError):
        compose_strategy_spec(prompt="", name="")
    spec = compose_strategy_spec(prompt="Mean reversion for gold, risk 10%", name="Gold strategy",
                                 asset="commodity", symbol="XAU_USD", timeframe="4h")
    assert spec["strategy_family"] == "MEAN_REVERSION"
    assert spec["risk_per_trade"] <= 0.005
    assert any("capped" in item.lower() for item in spec["validation_warnings"])


def test_current_strategy_ui_describes_local_native_ai():
    customer = (ROOT / "app" / "templates" / "customer.html").read_text()
    assert "Atlas Native AI" in customer
    assert "Generate Native AI strategy" in customer
    assert "uses a real AI model" not in customer


def test_deployment_scripts_do_not_require_hosted_ai_provider_keys():
    paths = ["deploy/cloud-run-deploy.sh", "deploy/cloud-run-worker-deploy.sh",
             "deploy/gcp-bootstrap.sh", "deploy/cloud-run.env.example",
             ".github/workflows/production-deploy.yml"]
    for path in paths:
        text = (ROOT / path).read_text()
        for token in ("GEMINI_API_KEY", "GEMINI_MODEL", "GROQ_API_KEY", "CEREBRAS_API_KEY", "atlas-gemini-api-key"):
            assert token not in text, path



def test_native_strategy_parser_preserves_multitimeframe_rules_and_avoids_substring_symbols():
    from app.native_ai import compose_strategy_spec

    spec = compose_strategy_spec(
        prompt="15m breakout with 1h confirmation for forex trend together",
        name="Multi-timeframe forex",
        asset="forex",
        timeframe="1h",
    )
    assert spec["symbols"] == ["EUR_USD"]
    assert spec["strategy_family"] == "BREAKOUT"
    assert set(spec["timeframes"]) == {"15m", "1h"}
    condition_timeframes = {row["type"]: row["timeframe"] for row in spec["entry_conditions"]}
    assert condition_timeframes["RANGE_BREAKOUT"] == "15m"
    assert condition_timeframes["BREAKOUT_QUALITY"] == "1h"


def test_native_strategy_parser_recognizes_ensemble_and_short_intent():
    from app.native_ai import compose_strategy_spec

    ensemble = compose_strategy_spec(
        prompt="combine momentum and trend strategies for BTC",
        name="Combined strategy",
    )
    assert ensemble["strategy_family"] == "ENSEMBLE"

    short = compose_strategy_spec(
        prompt="short the BTC market on a 1h timeframe",
        name="Short BTC",
    )
    assert short["direction"] == "SHORT"

    ambiguous = compose_strategy_spec(prompt="BTC strategy on 1h", name="Unspecified direction")
    assert any("direction was explicit" in item.lower() for item in ambiguous["validation_warnings"])


def test_worker_deployment_pins_explicit_gcp_project():
    script = (ROOT / "deploy" / "cloud-run-worker-deploy.sh").read_text()
    assert '--project "$GCP_PROJECT_ID"' in script


def test_strategy_candidate_backtest_uses_the_requested_stop_and_target_parameters():
    from app.main import _candidate_backtest_config

    config = _candidate_backtest_config({"stop_loss_atr": 1.0, "take_profit_rr": 3.0})
    assert config.stop_atr == 1.0
    assert config.take_profit_atr == 3.0

    with pytest.raises(ValueError):
        _candidate_backtest_config({"stop_loss_atr": float("nan"), "take_profit_rr": 3.0})
    with pytest.raises(ValueError):
        _candidate_backtest_config({"stop_loss_atr": 1.0})
