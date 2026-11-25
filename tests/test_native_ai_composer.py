"""Native AI composer regression tests using a labelled local corpus."""
import json
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



def test_benchmark_reports_field_accuracy_and_output_contract_without_prompt_capture():
    report = _EVAL.evaluate(ROOT / "app" / "native_ai.py")
    assert report["expectations_met"] == report["expectations"]
    assert report["expectation_accuracy"] == 1.0
    assert report["case_exact_accuracy"] == 1.0
    assert report["output_contract_rate"] == 1.0
    assert report["dataset"]["dataset_type"] == "self_authored_synthetic"
    assert report["dataset"]["raw_prompts_in_report"] is False
    assert report["comparison"]["status"] == "NOT_RUN"


def test_real_user_benchmark_requires_consent_and_anonymisation(tmp_path):
    import json

    dataset = tmp_path / "real_user_prompts.json"
    dataset.write_text(json.dumps({
        "schema_version": 1,
        "metadata": {
            "dataset_type": "real_user_consented",
            "source": "opt-in anonymised strategy-lab sample",
            "consent_confirmed": False,
            "anonymized": True,
        },
        "cases": [{
            "id": "sample-1",
            "prompt": "BTC breakout on 1h",
            "expect": {"asset": "crypto"},
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="consent_confirmed=true"):
        _EVAL.evaluate(ROOT / "app" / "native_ai.py", dataset)


def test_consent_attested_real_user_benchmark_is_scored_without_echoing_prompts(tmp_path):
    import json

    prompt = "BTC breakout on 1h"
    dataset = tmp_path / "real_user_prompts.json"
    dataset.write_text(json.dumps({
        "schema_version": 1,
        "metadata": {
            "dataset_type": "real_user_consented",
            "source": "opt-in anonymised strategy-lab sample",
            "consent_confirmed": True,
            "anonymized": True,
        },
        "cases": [{
            "id": "sample-1",
            "prompt": prompt,
            "expect": {
                "asset": "crypto",
                "symbol": "BTC/USDT",
                "strategy_family": "BREAKOUT",
                "timeframes": ["1h"],
            },
        }],
    }), encoding="utf-8")

    report = _EVAL.evaluate(ROOT / "app" / "native_ai.py", dataset)
    rendered = json.dumps(report)
    assert report["cases_passed"] == 1
    assert report["expectation_accuracy"] == 1.0
    assert report["dataset"]["dataset_type"] == "real_user_consented"
    assert report["dataset"]["provenance_status"] == "submitter_attested"
    assert report["dataset"]["raw_prompts_in_report"] is False
    assert prompt not in rendered


def test_comparator_predictions_are_paired_to_exact_dataset_fingerprint(tmp_path):
    import hashlib
    import json

    from app.native_ai import compose_strategy_spec

    dataset = tmp_path / "paired_prompts.json"
    dataset.write_text(json.dumps([{
        "id": "case-1",
        "prompt": "BTC breakout on 1h",
        "expect": {"asset": "crypto", "strategy_family": "BREAKOUT"},
    }]), encoding="utf-8")
    output = compose_strategy_spec(prompt="BTC breakout on 1h", name="Comparator output")
    comparison = tmp_path / "comparison.json"
    comparison.write_text(json.dumps({
        "schema_version": 1,
        "system": "Reference parser v1",
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "predictions": [{"id": "case-1", "output": output}],
    }), encoding="utf-8")

    report = _EVAL.evaluate(ROOT / "app" / "native_ai.py", dataset, comparison)
    assert report["comparison"]["status"] == "SCORED"
    assert report["comparison"]["system"] == "Reference parser v1"
    assert report["comparison"]["metrics"]["case_exact_accuracy"] == 1.0

    mismatched = json.loads(comparison.read_text(encoding="utf-8"))
    mismatched["dataset_sha256"] = "0" * 64
    comparison.write_text(json.dumps(mismatched), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        _EVAL.evaluate(ROOT / "app" / "native_ai.py", dataset, comparison)


def test_missing_predictions_count_as_failed_expected_labels():
    cases = [{
        "id": "missing",
        "prompt": "BTC breakout on 1h",
        "expect": {"asset": "crypto", "warns": ["confirm direction"]},
    }]
    scored = _EVAL._score_predictions(cases, {})
    assert scored["expectations"] == 2
    assert scored["expectations_met"] == 0
    assert scored["expectation_accuracy"] == 0.0
    assert scored["cases_passed"] == 0


def test_benchmark_contract_rejects_blank_or_malformed_conditions():
    spec = compose_strategy_spec(prompt="BTC breakout on 1h", name="Condition contract")
    spec["entry_conditions"] = [{"type": "", "rule": ""}]
    spec["exit_conditions"] = [{"type": "RSI", "rule": "   "}]
    failures = _EVAL._contract_failures(spec)
    assert "entry conditions violate the Atlas strategy output contract" in failures
    assert "exit conditions violate the Atlas strategy output contract" in failures


def test_malformed_condition_timeframe_is_scored_as_invalid_not_raised():
    spec = compose_strategy_spec(
        prompt="BTC breakout on 1h",
        name="Malformed timeframe",
    )
    spec["exit_conditions"][0]["timeframe"] = {"unexpected": "object"}

    failures = _EVAL._contract_failures(spec)

    assert "exit conditions violate the Atlas strategy output contract" in failures


def test_condition_text_is_normalized_before_application_contract_scoring():
    spec = compose_strategy_spec(
        prompt="BTC breakout on 1h",
        name="Condition normalization",
    )
    spec["entry_conditions"][0]["type"] = "  RSI  "
    spec["entry_conditions"][0]["rule"] = "  close above 20  "

    failures = _EVAL._contract_failures(spec)

    assert failures == []
    normalized = AIStrategySpec.model_validate(spec)
    assert normalized.entry_conditions[0].type == "RSI"
    assert normalized.entry_conditions[0].rule == "close above 20"


def test_condition_text_that_is_only_whitespace_or_too_short_is_rejected():
    spec = compose_strategy_spec(
        prompt="BTC breakout on 1h",
        name="Short condition text",
    )
    spec["entry_conditions"][0]["type"] = " a "
    spec["entry_conditions"][0]["rule"] = " ab "

    failures = _EVAL._contract_failures(spec)

    assert "entry conditions violate the Atlas strategy output contract" in failures


def test_malformed_comparator_symbols_and_warnings_are_scored_not_raised():
    spec = compose_strategy_spec(prompt="BTC breakout on 1h", name="Malformed comparator")
    spec["symbols"] = "BTC/USDT"
    spec["validation_warnings"] = ["PRIVATE WARNING", 123]
    case = {
        "id": "malformed",
        "prompt": "redacted",
        "expect": {"symbol": "BTC/USDT"},
    }
    checks, failures = _EVAL._score_case(case, spec)
    assert checks == [{"field": "symbol", "passed": False}]
    assert any("symbols" in failure for failure in failures)
    assert any("validation warnings" in failure for failure in failures)
    assert "PRIVATE WARNING" not in " ".join(failures)


def test_mismatch_reports_never_echo_private_expected_or_actual_values():
    case = {
        "id": "private-case",
        "prompt": "PRIVATE PROMPT",
        "expect": {"asset": "PRIVATE EXPECTATION"},
    }
    prediction = compose_strategy_spec(prompt="BTC breakout on 1h", name="Private mismatch")
    prediction["asset"] = "PRIVATE ACTUAL"
    _checks, failures = _EVAL._score_case(case, prediction)
    rendered = json.dumps(failures)
    assert "PRIVATE PROMPT" not in rendered
    assert "PRIVATE EXPECTATION" not in rendered
    assert "PRIVATE ACTUAL" not in rendered


def test_dataset_rejects_cases_without_any_scored_assertion(tmp_path):
    import json

    dataset = tmp_path / "unlabelled.json"
    dataset.write_text(json.dumps([{
        "id": "case-empty",
        "prompt": "BTC breakout on 1h",
        "expect": {"warns": [], "no_warns": []},
    }]), encoding="utf-8")
    with pytest.raises(ValueError, match="at least one scored field"):
        _EVAL._load_dataset(dataset)


def test_evaluation_keeps_safe_generation_failure_type_and_redacts_source(tmp_path, monkeypatch):
    import json

    secret = "PRIVATE PROMPT AND SOURCE"
    dataset = tmp_path / "custom.json"
    dataset.write_text(json.dumps({
        "schema_version": 1,
        "metadata": {
            "dataset_type": "synthetic",
            "source": secret,
        },
        "cases": [{
            "id": "safe-case",
            "prompt": "BTC breakout on 1h",
            "expect": {"asset": "crypto"},
        }],
    }), encoding="utf-8")

    class BrokenComposer:
        @staticmethod
        def compose_strategy_spec(**_kwargs):
            raise ValueError(secret)

    monkeypatch.setattr(_EVAL, "_load", lambda _path: BrokenComposer)
    report = _EVAL.evaluate(Path("unused-module"), dataset)
    rendered = json.dumps(report)
    assert report["generation_failures"] == {"safe-case": "ValueError"}
    assert report["dataset"]["source"] == "user_supplied_dataset"
    assert secret not in rendered



def test_benchmark_output_contract_matches_application_title_risk_and_extra_field_rules():
    spec = compose_strategy_spec(prompt="BTC breakout on 1h", name="Contract parity")
    assert _EVAL._contract_failures(spec) == []

    invalid_title = dict(spec, title="")
    assert _EVAL._contract_failures(invalid_title)

    invalid_optional_risk = dict(spec, stop_loss_atr=11.0)
    assert _EVAL._contract_failures(invalid_optional_risk)

    forbidden_extra = dict(spec, unexpected_private_field="do not report this")
    failures = _EVAL._contract_failures(forbidden_extra)
    assert failures
    assert "unexpected_private_field" not in " ".join(failures)
    assert "do not report this" not in " ".join(failures)

@pytest.mark.parametrize(
    "field,value",
    [
        ("asset", True),
        ("asset", "equities"),
        ("symbol", 1),
        ("strategy_family", "SCALPING"),
        ("direction", "SIDEWAYS"),
        ("timeframes", "1h"),
        ("timeframes", ["2h"]),
        ("risk_per_trade", True),
        ("risk_per_trade", 0.1),
        ("stop_loss_atr", False),
        ("take_profit_rr", 11),
        ("sessions", "London"),
    ],
)
def test_custom_benchmark_rejects_invalid_expected_label_types_and_domains(tmp_path, field, value):
    dataset = tmp_path / "invalid-label.json"
    dataset.write_text(json.dumps([{
        "id": "invalid-label",
        "prompt": "BTC breakout on 1h",
        "expect": {field: value},
    }]), encoding="utf-8")

    with pytest.raises(ValueError, match=f"invalid expected label for {field}"):
        _EVAL._load_dataset(dataset)
