from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def test_alembic_graph_has_unique_revisions_valid_parents_and_one_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions())
    revision_ids = [revision.revision for revision in revisions]
    known = set(revision_ids)

    assert len(revision_ids) == len(known), "revision IDs must be unique even when numeric filename prefixes repeat"
    assert len(script.get_heads()) == 1, f"expected one Alembic head, got {script.get_heads()}"
    for revision in revisions:
        parents = revision.down_revision
        if parents is None:
            continue
        if isinstance(parents, str):
            parents = (parents,)
        assert all(parent in known for parent in parents), (
            f"revision {revision.revision} has missing parent(s): {parents}"
        )


def test_oos_rank_is_independent_of_full_sample_scores():
    from app.research_engine import _rank_by_oos

    rows = [
        {"strategy": "trend", "sharpe": 9.0, "total_return": 4.0},
        {"strategy": "momentum", "sharpe": 0.1, "total_return": 0.01},
        {"strategy": "breakout", "sharpe": 100.0, "total_return": 100.0},
    ]
    oos = {
        "trend": {"status": "OK", "oos_total_return": 0.01, "sharpe": 0.4},
        "momentum": {"status": "OK", "oos_total_return": 0.20, "sharpe": 0.3},
        "breakout": {"status": "INSUFFICIENT_DATA", "oos_total_return": 999.0, "sharpe": 999.0},
    }

    assert [row["strategy"] for row in _rank_by_oos(rows, oos)] == [
        "momentum", "trend", "breakout"
    ]


def test_research_summary_uses_oos_metrics_and_labels_their_evidence():
    from app.daily_research import summarize_research

    summary = summarize_research({
        "regime": {"regime": "RANGE", "annualized_realized_vol": 0.2},
        "research_order": ["trend", "momentum"],
        "strategies": [
            {"strategy": "trend", "sharpe": 8.0, "max_drawdown": 0.0, "total_return": 5.0},
            {"strategy": "momentum", "sharpe": 0.2, "max_drawdown": -0.4, "total_return": -0.5},
        ],
        "validation": {
            "trial_labels": [],
            "per_strategy_oos": {
                "trend": {"status": "INSUFFICIENT_DATA"},
                "momentum": {
                    "status": "OK", "sharpe": 0.7, "max_drawdown": -0.08,
                    "oos_total_return": 0.12,
                },
            },
        },
        "universe_bias": "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY",
    })

    assert summary["top_strategy"] == "momentum"
    assert summary["top_strategy_basis"] == "OUT_OF_SAMPLE_MEASUREMENT_ONLY"
    assert summary["top_strategy_oos_status"] == "OK"
    assert summary["sharpe"] == 0.7
    assert summary["max_drawdown"] == -0.08
    assert summary["total_return"] == 0.12
    assert summary["universe_bias"] == "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY"


def test_invalid_crypto_research_result_exposes_survivorship_limitation():
    from app.research_engine import backtest_all_strategies

    result = backtest_all_strategies(pd.DataFrame({"close": [100.0]}), include_ai=False, asset="crypto")

    assert result["status"] == "DATA_INVALID"
    assert result["universe_bias"] == "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY"
    assert result["data_quality"]["universe_bias"] == result["universe_bias"]
