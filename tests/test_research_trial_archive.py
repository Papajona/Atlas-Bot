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
