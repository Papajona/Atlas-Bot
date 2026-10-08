import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import app.trading_core as tc


def _frame(n=100):
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    close = np.linspace(100.0, 110.0, n)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
        },
        index=idx,
    )


def test_train_model_persists_final_model_class_mapping_when_one_class_is_absent(tmp_path, monkeypatch):
    df = _frame()
    features = pd.DataFrame(
        {
            "f1": np.sin(np.arange(len(df)) / 7.0),
            "f2": np.cos(np.arange(len(df)) / 11.0),
        },
        index=df.index,
    )

    monkeypatch.setattr(tc, "build_features", lambda _df: features)
    # Economic labels are only SHORT (-1) and FLAT (0); LONG is genuinely absent.
    monkeypatch.setattr(
        tc,
        "triple_barrier",
        lambda close, vol, high, low, max_hold: pd.Series(
            np.where(np.arange(len(close)) % 2 == 0, -1, 0),
            index=close.index,
            dtype=float,
        ),
    )

    out = tc.train_model(df, str(tmp_path / "model.joblib"), min_train=20, folds=2)
    assert out["folds"] >= 1

    meta_path = Path(str(tmp_path / "model.joblib") + ".meta.json")
    meta = json.loads(meta_path.read_text())
    assert meta["classes"] == [-1, 0, 1]
    assert meta["model_local_classes"] == [0, 1]

    prediction = tc.predict_latest(df, str(tmp_path / "model.joblib"))
    assert prediction["long_probability"] == 0.0
    assert 0.0 <= prediction["short_probability"] <= 1.0
    assert 0.0 <= prediction["flat_probability"] <= 1.0
    assert prediction["short_probability"] + prediction["flat_probability"] == pytest.approx(1.0)
