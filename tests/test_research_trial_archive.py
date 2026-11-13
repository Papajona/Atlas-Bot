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



def test_fixed_signal_trial_archive_sharpe_uses_exact_capped_dsr_sample():
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
            "fixed_signal_oos": {"annualization_factor": 252},
        },
    }
    sample = [0.01, 0.02, -0.01, 0.025]
    metrics = {
        "status": "OK",
        "sharpe": 99.0,
        "annualization_factor": 252,
        "oos_return_sample": sample,
        "oos_total_return": 0.04,
        "max_drawdown": -0.02,
        "trades": 8,
    }

    row = _row_payload(report, context, "component:ensemble", metrics, "ensemble", "evaluation")
    mean = sum(sample) / len(sample)
    variance = sum((value - mean) ** 2 for value in sample) / (len(sample) - 1)
    expected = mean / variance ** 0.5 * 252 ** 0.5

    assert abs(row["annualized_sharpe"] - expected) < 1e-12
    assert row["annualized_sharpe"] != 99.0
    assert row["archive_status"] == "RECORDED_UNVERIFIED"


def test_fixed_signal_trial_archive_without_capped_sample_is_unverified():
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
            "fixed_signal_oos": {"annualization_factor": 252},
        },
    }
    metrics = {
        "status": "OK",
        "sharpe": 1.5,
        "annualization_factor": 252,
        "oos_total_return": 0.04,
        "max_drawdown": -0.02,
        "trades": 8,
    }

    row = _row_payload(report, context, "component:ensemble", metrics, "ensemble", "evaluation")

    assert row["annualized_sharpe"] is None
    assert row["archive_status"] == "UNVERIFIED_MISSING_METRICS"



def test_grid_variant_archive_uses_evaluated_variant_configuration_hash():
    from app.research_trial_archive import _row_payload

    context = {
        "asset_class": "crypto",
        "venue": "bybit",
        "market_type": "perpetual",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "dataset_sha256": "a" * 64,
        "protocol_version": "atlas-purged-walk-forward-v3",
    }
    variant_sha = "d" * 64
    report = {
        "config": {"signal_threshold": 0.05, "stop_atr": 1.5},
        "code_sha256": "c" * 64,
        "validation": {
            "oos_sample_sha256": "b" * 64,
            "protocol_version": "atlas-purged-walk-forward-v3",
            "fixed_signal_oos": {"annualization_factor": 8760},
        },
    }
    metrics = {
        "config_sha256": variant_sha,
        "threshold_multiplier": 1.2,
        "risk_multiplier": 0.8,
        "oos_status": "OK",
        "oos_sharpe": 0.8,
        "oos_total_return": 0.15,
        "oos_max_drawdown": -0.04,
        "oos_trades": 22,
        "annualization_factor": 8760,
        "oos_return_sample": [0.01, 0.02, -0.01, 0.025],
    }
    row = _row_payload(
        report, context, "variant:t1.20:r0.80@" + variant_sha,
        metrics, "variant", "evaluation",
    )
    assert row["config_sha256"] == variant_sha
    assert row["archive_status"] == "RECORDED_UNVERIFIED"
