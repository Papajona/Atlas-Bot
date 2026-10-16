import math

import pytest

from app.meta_labeling import _oos_classification_metrics


def test_oos_meta_filter_metrics_compare_selected_signals_with_unfiltered_baseline():
    result = _oos_classification_metrics(
        y_true=[1, 0, 1, 0],
        probabilities=[0.90, 0.80, 0.70, 0.10],
        threshold=0.50,
    )

    # The filter selects three of four candidate signals; two are successful.
    assert result["oos_observations"] == 4
    assert result["oos_predicted_positive"] == 3
    assert result["oos_true_positive"] == 2
    assert result["oos_false_positive"] == 1
    assert result["oos_false_negative"] == 0
    assert math.isclose(result["oos_take_rate"], 0.75)
    assert math.isclose(result["oos_precision"], 2 / 3)
    assert math.isclose(result["oos_recall"], 1.0)
    assert math.isclose(result["oos_filtered_win_rate"], 2 / 3)
    assert math.isclose(result["oos_unfiltered_win_rate"], 0.5)
    assert math.isclose(result["oos_win_rate_lift"], 1 / 6)


def test_oos_meta_filter_uses_null_not_fake_zero_when_no_trades_are_selected():
    result = _oos_classification_metrics(
        y_true=[1, 0],
        probabilities=[0.10, 0.20],
        threshold=0.50,
    )

    assert result["oos_take_rate"] == 0.0
    assert result["oos_precision"] is None
    assert result["oos_filtered_win_rate"] is None
    assert result["oos_win_rate_lift"] is None
    assert result["oos_recall"] == 0.0


@pytest.mark.parametrize(
    "labels, probabilities, threshold",
    [
        ([0, 1], [0.2], 0.5),
        ([0, 2], [0.2, 0.8], 0.5),
        ([0, 1], [0.2, float("nan")], 0.5),
        ([0, 1], [0.2, 0.8], 1.5),
    ],
)
def test_oos_meta_filter_rejects_misaligned_or_invalid_metrics_inputs(
    labels, probabilities, threshold
):
    with pytest.raises(ValueError):
        _oos_classification_metrics(labels, probabilities, threshold)


def test_empty_oos_metrics_are_explicitly_unavailable():
    result = _oos_classification_metrics([], [], 0.5)

    assert result["oos_observations"] == 0
    assert result["oos_precision"] is None
    assert result["oos_recall"] is None
    assert result["oos_filtered_win_rate"] is None
    assert result["oos_unfiltered_win_rate"] is None
    assert result["oos_win_rate_lift"] is None



@pytest.mark.asyncio
async def test_meta_label_research_route_exposes_diagnostics_without_execution_authority(monkeypatch):
    import pandas as pd
    import app.main as main
    from app.schemas import MarketRequest

    df = pd.DataFrame(
        {"open":[100.0, 101.0], "high":[102.0, 103.0],
         "low":[99.0, 100.0], "close":[101.0, 102.0], "volume":[10.0, 12.0]},
        index=pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"),
    )
    df.attrs["data_provenance"] = {
        "source":"synthetic_test", "symbol":"BTC/USDT", "exchange":"binance",
        "timeframe":"1h", "row_count":2, "sha256":"a"*64,
    }

    async def fake_auth(*args, **kwargs):
        return {"sub":"research-test"}

    async def fake_require_role(*args, **kwargs):
        return None

    async def fake_audit(*args, **kwargs):
        return None

    def fake_meta_report(frame, **kwargs):
        return {
            "status":"OK", "oos_observations":100, "oos_precision":0.61,
            "oos_recall":0.57, "oos_filtered_win_rate":0.60,
            "oos_unfiltered_win_rate":0.51, "oos_win_rate_lift":0.09,
            "execution_authority":True,
        }

    monkeypatch.setattr(main, "auth", fake_auth)
    monkeypatch.setattr(main, "require_role", fake_require_role)
    monkeypatch.setattr(main, "_audit", fake_audit)
    monkeypatch.setattr(main, "market_data", lambda req: df)
    monkeypatch.setattr(main, "meta_label_walk_forward", fake_meta_report)

    result = await main.meta_label_research_api(MarketRequest(), None, None)
    assert result["status"] == "OK"
    assert result["oos_precision"] == pytest.approx(0.61)
    assert result["oos_recall"] == pytest.approx(0.57)
    assert result["oos_filtered_win_rate"] == pytest.approx(0.60)
    assert result["oos_unfiltered_win_rate"] == pytest.approx(0.51)
    assert result["oos_win_rate_lift"] == pytest.approx(0.09)
    assert result["execution_authority"] is False
    assert "do not bypass" in result["evidence_note"]
