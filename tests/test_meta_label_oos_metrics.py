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
