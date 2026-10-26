from __future__ import annotations

from app.research_trial_archive import _row_payload


def test_incomplete_trial_attempts_are_retained_with_unique_evaluation_ids():
    context = {
        "asset_class": "",
        "venue": "",
        "market_type": "",
        "symbol": "",
        "timeframe": "",
        "dataset_sha256": "",
    }

    first = _row_payload({}, context, "strategy@attempt", {}, "strategy", "evaluation-a")
    second = _row_payload({}, context, "strategy@attempt", {}, "strategy", "evaluation-b")

    assert first["archive_status"] == "UNVERIFIED_MISSING_CONTEXT"
    assert first["trial_key"] != second["trial_key"]
    assert first["code_sha256"] == ""
    assert first["dataset_sha256"] == ""



def test_ai_trial_config_changes_archive_digest_and_idempotency_key():
    context = {
        "asset_class": "forex",
        "venue": "oanda",
        "market_type": "practice",
        "symbol": "EUR_USD",
        "timeframe": "1h",
        "dataset_sha256": "a" * 64,
        "protocol_version": "atlas-purged-walk-forward-v2",
    }
    report = {
        "config": {"signal_threshold": 0.1},
        "code_sha256": "c" * 64,
        "validation": {
            "oos_sample_sha256": "b" * 64,
            "protocol_version": "atlas-purged-walk-forward-v2",
        },
    }
    base_metrics = {
        "trial_config": {
            "ai_threshold": 0.05,
            "stop_atr": 1.0,
            "take_profit_atr": 2.0,
            "model": {"n_estimators": 300},
        },
        "oos_sample_sha256": "d" * 64,
        "protocol_version": "atlas-purged-walk-forward-v2",
        "status": "OK",
        "sharpe": 0.4,
        "oos_total_return": 0.12,
        "max_drawdown": -0.08,
        "trades": 15,
    }
    changed_metrics = {
        **base_metrics,
        "trial_config": {
            **base_metrics["trial_config"],
            "ai_threshold": 0.10,
        },
    }
    first = _row_payload(report, context, "ai@config", base_metrics, "ai", "evaluation-a")
    second = _row_payload(report, context, "ai@config", changed_metrics, "ai", "evaluation-b")
    assert first["config_sha256"] != second["config_sha256"]
    assert first["trial_key"] != second["trial_key"]
