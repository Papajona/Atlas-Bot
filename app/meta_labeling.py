from __future__ import annotations

import math

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .strategy_engine import strategy_signals

META_FEATURES = [
    "ensemble",
    "trend",
    "momentum",
    "breakout",
    "mean_reversion",
    "adx",
    "rsi",
    "realized_vol",
]


def build_meta_dataset(
    df: pd.DataFrame,
    *,
    horizon: int = 3,
    min_edge_bps: float = 10.0,
) -> tuple[pd.DataFrame, pd.Series]:
    """Build features at decision time and labels from a fully completed future horizon."""
    if isinstance(horizon, bool) or not isinstance(horizon, (int, np.integer)) or horizon < 1:
        raise ValueError("horizon must be a positive integer")
    edge_threshold = float(min_edge_bps)
    if not math.isfinite(edge_threshold) or edge_threshold < 0:
        raise ValueError("min_edge_bps must be finite and non-negative")
    if not df.index.is_unique or not df.index.is_monotonic_increasing:
        raise ValueError("meta-label data index must be unique and chronological")

    signals = strategy_signals(df)
    close = df["close"].astype(float)
    forward_return = close.shift(-int(horizon)) / close - 1.0
    direction = np.sign(signals["ensemble"].astype(float))
    edge = forward_return * direction
    target = (edge > edge_threshold / 10_000.0).astype(int)
    features = signals[META_FEATURES].replace([np.inf, -np.inf], np.nan)
    valid = features.notna().all(axis=1) & forward_return.notna() & (direction != 0)
    return features.loc[valid], target.loc[valid]


def _oos_classification_metrics(
    y_true: np.ndarray | list[int],
    probabilities: np.ndarray | list[float],
    threshold: float,
) -> dict:
    """Measure the filter only on held-out predictions, versus taking every eligible signal."""
    labels = np.asarray(y_true, dtype=int).reshape(-1)
    probs = np.asarray(probabilities, dtype=float).reshape(-1)
    if len(labels) != len(probs):
        raise ValueError("OOS labels and probabilities must have equal lengths")
    if labels.size == 0:
        return {
            "oos_observations": 0,
            "oos_take_rate": None,
            "oos_precision": None,
            "oos_recall": None,
            "oos_filtered_win_rate": None,
            "oos_unfiltered_win_rate": None,
            "oos_win_rate_lift": None,
            "oos_accuracy": None,
            "oos_f1": None,
            "oos_predicted_positive": 0,
            "oos_true_positive": 0,
            "oos_false_positive": 0,
            "oos_false_negative": 0,
        }
    if not np.isfinite(probs).all() or not np.isin(labels, [0, 1]).all():
        raise ValueError("OOS labels and probabilities must be finite binary labels and finite scores")
    if not math.isfinite(float(threshold)) or not 0.0 <= float(threshold) <= 1.0:
        raise ValueError("threshold must be between zero and one")

    predicted = probs >= float(threshold)
    positive = labels == 1
    tp = int(np.count_nonzero(predicted & positive))
    fp = int(np.count_nonzero(predicted & ~positive))
    fn = int(np.count_nonzero(~predicted & positive))
    tn = int(np.count_nonzero(~predicted & ~positive))
    predicted_count = tp + fp
    positive_count = tp + fn
    precision = tp / predicted_count if predicted_count else None
    recall = tp / positive_count if positive_count else None
    filtered_win_rate = float(labels[predicted].mean()) if predicted_count else None
    unfiltered_win_rate = float(labels.mean())
    lift = (
        filtered_win_rate - unfiltered_win_rate
        if filtered_win_rate is not None
        else None
    )
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else None
    )
    return {
        "oos_observations": int(labels.size),
        "oos_take_rate": float(predicted.mean()),
        "oos_precision": precision,
        "oos_recall": recall,
        "oos_filtered_win_rate": filtered_win_rate,
        "oos_unfiltered_win_rate": unfiltered_win_rate,
        "oos_win_rate_lift": lift,
        "oos_accuracy": (tp + tn) / int(labels.size),
        "oos_f1": f1,
        "oos_predicted_positive": predicted_count,
        "oos_true_positive": tp,
        "oos_false_positive": fp,
        "oos_false_negative": fn,
    }


def meta_label_walk_forward(
    df: pd.DataFrame,
    *,
    horizon: int = 3,
    min_edge_bps: float = 10.0,
    folds: int = 5,
    threshold: float = 0.55,
) -> dict:
    """Report out-of-sample meta-filter performance and score the current unlabeled bar.

    Purging and embargo are measured in original OHLCV-bar positions, not in the
    filtered feature-row count. A sparse signal series must not shorten the label
    horizon and allow a training outcome to overlap the next test window.
    """
    if isinstance(horizon, bool) or not isinstance(horizon, (int, np.integer)) or horizon < 1:
        return {"status": "INVALID_CONFIG", "reason": "horizon_must_be_positive_integer", "execution_authority": False}
    if isinstance(folds, bool) or not isinstance(folds, (int, np.integer)) or folds < 2:
        return {"status": "INVALID_CONFIG", "reason": "folds_must_be_integer_at_least_two", "execution_authority": False}
    if not math.isfinite(float(threshold)) or not 0.0 <= float(threshold) <= 1.0:
        return {"status": "INVALID_CONFIG", "reason": "threshold_must_be_between_zero_and_one", "execution_authority": False}
    if not df.index.is_unique or not df.index.is_monotonic_increasing:
        return {"status": "INVALID_DATA", "reason": "index_must_be_unique_and_chronological", "execution_authority": False}

    try:
        features, target = build_meta_dataset(
            df, horizon=int(horizon), min_edge_bps=min_edge_bps
        )
        source_positions = df.index.get_indexer(features.index)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return {"status": "INVALID_DATA", "reason": str(exc), "execution_authority": False}

    if (source_positions < 0).any() or len(features) != len(target):
        return {"status": "INVALID_DATA", "reason": "feature_label_index_mismatch", "execution_authority": False}
    if len(features) < 250 or target.nunique() < 2:
        return {"status": "INSUFFICIENT_DATA", "observations": int(len(features)), "execution_authority": False}

    fold_count = max(2, min(int(folds), len(features) // 50))
    splits = np.array_split(np.arange(len(features)), fold_count)
    oos_probabilities: list[float] = []
    oos_labels: list[int] = []
    used_folds = 0
    label_horizon = int(horizon)

    for fold_number in range(1, len(splits)):
        train_candidates = np.concatenate(splits[:fold_number])
        test_candidates = splits[fold_number]
        if len(test_candidates) == 0:
            continue

        test_start_bar = int(source_positions[test_candidates[0]])
        # A training label at position p observes the close at p + horizon.
        # That endpoint must precede the first held-out decision bar.
        train = train_candidates[
            source_positions[train_candidates] + label_horizon < test_start_bar
        ]
        # Embargo the first horizon *source bars*, not merely N rows after
        # dropping neutral/invalid signals from the feature matrix.
        test = test_candidates[
            source_positions[test_candidates] >= test_start_bar + label_horizon
        ]
        if len(train) < 100 or len(test) == 0 or target.iloc[train].nunique() < 2:
            continue

        model = make_pipeline(
            StandardScaler(),
            LogisticRegression(
                solver="liblinear",
                max_iter=500,
                class_weight="balanced",
                random_state=7,
            ),
        )
        model.fit(features.iloc[train], target.iloc[train])
        probabilities = model.predict_proba(features.iloc[test])[:, 1]
        oos_probabilities.extend(float(value) for value in probabilities)
        oos_labels.extend(int(value) for value in target.iloc[test].to_numpy())
        used_folds += 1

    if not oos_labels:
        return {
            "status": "VALIDATION_UNAVAILABLE",
            "reason": "no_usable_purged_oos_folds",
            "observations": int(len(features)),
            "oos_observations": 0,
            "execution_authority": False,
        }

    try:
        oos_metrics = _oos_classification_metrics(oos_labels, oos_probabilities, threshold)
    except ValueError as exc:
        return {
            "status": "VALIDATION_UNAVAILABLE",
            "reason": str(exc),
            "observations": int(len(features)),
            "oos_observations": int(len(oos_labels)),
            "execution_authority": False,
        }

    # Fit the operational estimator only on completed labels. The current candle's
    # features are not in the labelled matrix because its future outcome is unknown.
    latest_model = make_pipeline(
        StandardScaler(),
        LogisticRegression(
            solver="liblinear",
            max_iter=500,
            class_weight="balanced",
            random_state=7,
        ),
    )
    latest_model.fit(features, target)
    current_features = strategy_signals(df)[META_FEATURES].replace(
        [np.inf, -np.inf], np.nan
    )
    current = current_features.iloc[[-1]]
    if current.isna().any(axis=None):
        return {
            "status": "CURRENT_FEATURES_UNAVAILABLE",
            "observations": int(len(features)),
            "oos_folds_used": used_folds,
            **oos_metrics,
            "execution_authority": False,
        }

    latest_probability = float(latest_model.predict_proba(current)[0, 1])
    last_label_position = int(source_positions[-1])
    label_end_position = min(last_label_position + label_horizon, len(df) - 1)
    return {
        "status": "OK",
        "observations": int(len(features)),
        "oos_folds_used": used_folds,
        **oos_metrics,
        "current_probability": latest_probability,
        "latest_probability": latest_probability,
        "threshold": float(threshold),
        "horizon_bars": label_horizon,
        "min_edge_bps": float(min_edge_bps),
        "training_last_label_index": str(features.index[-1]),
        "training_last_label_end_index": str(df.index[label_end_position]),
        "prediction_index": str(current.index[-1]),
        "current_prediction_is_oos": True,
        "current_prediction_has_realized_outcome": False,
        "current_prediction_evaluation": "UNLABELED_FORWARD_PREDICTION",
        "execution_authority": False,
    }


def meta_label_gate(
    df: pd.DataFrame,
    *,
    horizon: int = 3,
    min_edge_bps: float = 10.0,
    threshold: float = 0.55,
) -> dict:
    result = meta_label_walk_forward(
        df, horizon=horizon, min_edge_bps=min_edge_bps, threshold=threshold
    )
    if result.get("status") != "OK":
        return {**result, "take_trade": False}
    probability = float(result.get("latest_probability", 0.0))
    return {**result, "take_trade": probability >= float(threshold)}
