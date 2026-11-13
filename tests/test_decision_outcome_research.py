from __future__ import annotations

import pandas as pd

from app.decision_outcome_research import evaluate_decision_outcome_labels


def _label(position: int, *, decision: str | None = None, side: str | None = "buy", value: float | None = None):
    timestamp = pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(hours=position)
    decision = decision or ("NO_TRADE" if position % 2 else "TRADE")
    if value is None:
        value = float(position - 8)
    return {
        "record_type": "adaptive_decision_outcome_label",
        "label_version": "decision-outcome-v1",
        "status": "VALID",
        "decision_id": f"decision-{position}",
        "original_decision": decision,
        "mode": "PAPER",
        "asset": "crypto",
        "symbol": "BTC/USDT:USDT",
        "exchange": "bybit",
        "timeframe": "1h",
        "market_timestamp": timestamp.isoformat(),
        "decision_stage": "adaptive_model_confirmation" if decision == "NO_TRADE" else "execution",
        "decision_reason_code": "model_veto" if decision == "NO_TRADE" else "",
        "candidate_side": side,
        "strategy": "trend",
        "cost_source": "configured_asset_profile",
        "cost_bps_per_side": 3.5,
        "carry_bps_per_bar": 0.25,
        "regime": "TREND_UP",
        "horizons_bars": [1],
        "outcomes": {
            "1": {
                "status": "VALID",
                "horizon_bars": 1,
                "candidate_side_net_return_bps": value if side else None,
                "outcome_bar_open": (timestamp + pd.Timedelta(hours=1)).isoformat(),
            }
        },
    }


def test_report_separates_trade_and_rejected_signal_outcomes():
    labels = [_label(i) for i in range(20)]
    report = evaluate_decision_outcome_labels(
        labels, holdout_fraction=0.30, min_group_samples=3, min_unique_timestamps=10
    , redact_holdout=False)

    assert report["research_only"] is True
    assert report["automatic_promotion"] is False
    assert report["coverage"]["valid_labels"] == 20
    assert report["coverage"]["decision_counts"] == {"NO_TRADE": 10, "TRADE": 10}
    assert len(report["by_market_and_decision"]) == 1
    group = report["by_market_and_decision"][0]
    assert group["by_decision"]["NO_TRADE"]["n"] == 10
    assert group["by_decision"]["TRADE"]["n"] == 10
    assert group["by_decision"]["NO_TRADE"]["mean_bps"] is not None
    assert report["rejected_by_reason"][0]["decision_reason_code"] == "model_veto"
    assert report["rejected_by_reason"][0]["metrics"]["n"] == 10


def test_chronological_holdout_purges_training_labels_whose_outcome_overlaps():
    labels = [_label(i) for i in range(20)]
    report = evaluate_decision_outcome_labels(
        labels, holdout_fraction=0.30, min_group_samples=3, min_unique_timestamps=10
    , redact_holdout=False)
    split = report["out_of_sample"][0]

    assert split["status"] == "CHRONOLOGICAL_HOLDOUT"
    assert split["holdout_start"] == "2026-01-01T14:00:00+00:00"
    assert split["n_train_before_purge"] == 14
    assert split["n_purged_train_labels"] == 2
    assert split["n_train_after_purge"] == 12
    assert split["n_holdout"] == 6
    assert split["holdout_by_decision"]["TRADE"]["n"] == 3
    assert split["holdout_by_decision"]["NO_TRADE"]["n"] == 3
    assert split["holdout_by_decision"]["TRADE"]["sample_sufficient"] is True


def test_labels_without_candidate_side_are_counted_but_not_given_invented_returns():
    report = evaluate_decision_outcome_labels(
        [_label(0, side=None, value=None)],
        holdout_fraction=0.30,
        min_group_samples=1,
        min_unique_timestamps=3,
    , redact_holdout=False)
    assert report["coverage"]["valid_labels_without_candidate_side"] == 1
    assert report["coverage"]["candidate_side_counts"] == {"UNAVAILABLE": 1}
    assert report["by_market_and_decision"] == []
    assert report["out_of_sample"] == []


def test_small_groups_are_explicitly_marked_insufficient_for_holdout():
    report = evaluate_decision_outcome_labels(
        [_label(i) for i in range(5)],
        holdout_fraction=0.30,
        min_group_samples=3,
        min_unique_timestamps=10,
    , redact_holdout=False)
    assert report["out_of_sample"][0]["status"] == "INSUFFICIENT_UNIQUE_TIMESTAMPS"
    assert report["out_of_sample"][0]["minimum_unique_timestamps"] == 10


def test_non_valid_and_unrelated_records_are_not_used_as_outcomes():
    labels = [_label(i) for i in range(10)]
    labels.append({**_label(10), "status": "UNSUPPORTED"})
    labels.append({"record_type": "other_event", "status": "VALID"})
    report = evaluate_decision_outcome_labels(labels, min_group_samples=1, min_unique_timestamps=3, redact_holdout=False)

    assert report["coverage"]["valid_labels"] == 10
    assert report["coverage"]["ignored_non_valid_or_non_decision_labels"] == 1
    assert report["coverage"]["invalid_records"] == 1


def test_aggregation_separates_instruments_and_exchanges():
    labels = [
        {**_label(i), "symbol": "BTC/USDT", "exchange": "bybit"}
        for i in range(10)
    ] + [
        {**_label(i + 10), "symbol": "BTC/USDT", "exchange": "binance"}
        for i in range(10)
    ]
    report = evaluate_decision_outcome_labels(
        labels, min_group_samples=1, min_unique_timestamps=3
    , redact_holdout=False)
    groups = report["by_market_and_decision"]
    assert {(group["symbol"], group["exchange"]) for group in groups} == {
        ("BTC/USDT", "bybit"),
        ("BTC/USDT", "binance"),
    }


def test_duplicate_decision_label_is_counted_once_and_latest_record_wins():
    first = _label(0, decision="TRADE", value=1.0)
    latest = {
        **first,
        "outcomes": {"1": {**first["outcomes"]["1"], "candidate_side_net_return_bps": 9.0}},
    }
    other = _label(2, decision="TRADE", value=3.0)
    report = evaluate_decision_outcome_labels(
        [first, latest, other], min_group_samples=1, min_unique_timestamps=3
    , redact_holdout=False)
    assert report["coverage"]["input_records"] == 3
    assert report["coverage"]["valid_labels"] == 2
    assert report["coverage"]["duplicate_decision_labels"] == 1
    group = report["by_market_and_decision"][0]
    assert group["by_decision"]["TRADE"]["n"] == 2
    assert group["by_decision"]["TRADE"]["mean_bps"] == 6.0


def test_aggregation_does_not_mix_label_versions_or_cost_profiles():
    base = _label(0, decision="TRADE")
    alternative = {
        **_label(0, decision="TRADE"),
        "decision_id": "decision-other",
        "label_version": "decision-outcome-v2",
        "cost_source": "venue_calibration",
        "cost_bps_per_side": 7.0,
        "carry_bps_per_bar": 1.0,
    }
    report = evaluate_decision_outcome_labels(
        [base, alternative], min_group_samples=1, min_unique_timestamps=3
    , redact_holdout=False)
    groups = report["by_market_and_decision"]
    assert len(groups) == 2
    assert {
        (item["label_version"], item["cost_source"], item["cost_bps_per_side"], item["carry_bps_per_bar"])
        for item in groups
    } == {
        ("decision-outcome-v1", "configured_asset_profile", 3.5, 0.25),
        ("decision-outcome-v2", "venue_calibration", 7.0, 1.0),
    }



def test_holdout_metrics_are_redacted_by_default():
    report = evaluate_decision_outcome_labels(
        [_label(i) for i in range(20)],
        holdout_fraction=0.30,
        min_group_samples=3,
        min_unique_timestamps=10,
    )
    assert report["holdout_metrics_withheld"] is True
    assert report["out_of_sample"] == []
    assert report["research_only"] is True
