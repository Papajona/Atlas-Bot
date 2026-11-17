"""Regression tests for the 2026-11-17 executable-timing bias audit."""
from __future__ import annotations
import numpy as np
import pandas as pd
import pytest
from app import meta_labeling
from app.decision_outcome_research import _finite_return
from app.decision_outcomes import build_decision_outcome_label
from tests.test_decision_outcome_labels import _decision, _frame

def _gapped_frame(n=40, gap=0.01):
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    close = 100.0 * np.cumprod(np.full(n, 1.0005))
    open_ = np.r_[close[0], close[:-1]] * (1.0 + gap)
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.001,
                         "low": np.minimum(open_, close) * 0.999, "close": close,
                         "volume": np.full(n, 1000.0)}, index=idx)

def _stub_signals(df, long=True):
    cols = {name: 0.5 for name in meta_labeling.META_FEATURES}
    cols["ensemble"] = 1.0 if long else -1.0
    return pd.DataFrame(cols, index=df.index)

def test_meta_label_target_uses_next_open_not_signal_close(monkeypatch):
    df = _gapped_frame()
    monkeypatch.setattr(meta_labeling, "strategy_signals", lambda frame, *a, **k: _stub_signals(frame))
    X, y = meta_labeling.build_meta_dataset(df, horizon=2, min_edge_bps=0.0)
    expected = (df["open"].shift(-3) / df["open"].shift(-1) - 1.0) > 0.0
    expected = expected.loc[X.index].astype(int)
    assert (y.to_numpy() == expected.to_numpy()).all()
    legacy = (df["close"].shift(-2) / df["close"] - 1.0).loc[X.index] > 0.0
    assert legacy.all()

def test_meta_label_requires_open_column():
    with pytest.raises(ValueError, match="open column"):
        meta_labeling.build_meta_dataset(_gapped_frame().drop(columns=["open"]), horizon=2)

def test_meta_label_training_endpoint_accounts_for_next_open_entry():
    rng = np.random.default_rng(3)
    n = 700
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    open_ = np.r_[close[0], close[:-1]]
    df = pd.DataFrame({"open": open_, "high": close * 1.002, "low": close * 0.998,
                       "close": close, "volume": rng.uniform(100, 1000, n)}, index=idx)
    out = meta_labeling.meta_label_walk_forward(df, horizon=3, folds=5)
    assert out["status"] == "OK"
    assert out["label_timing"] == "NEXT_OPEN_ENTRY_TO_OPEN_AT_HORIZON"
    assert out["horizon_bars"] == 3
    last = df.index.get_loc(pd.Timestamp(out["training_last_label_index"]))
    assert pd.Timestamp(out["training_last_label_end_index"]) == df.index[min(last + 4, n - 1)]

def test_decision_outcome_adds_executable_next_open_returns():
    label = build_decision_outcome_label(_decision(), _frame())
    one = label["outcomes"]["1"]
    assert one["entry_timing"] == "NEXT_OPEN"
    assert one["executable_entry_open"] == pytest.approx(103.75)
    exe = (104.0 / 103.75 - 1.0) * 10_000.0
    assert one["executable_long_gross_return_bps"] == pytest.approx(exe)
    assert one["executable_candidate_side_net_return_bps"] == pytest.approx(exe - 15.125)
    assert one["candidate_side_net_return_bps"] > one["executable_candidate_side_net_return_bps"]

def test_research_prefers_executable_and_legacy_fallback_is_limited():
    new = build_decision_outcome_label(_decision(), _frame())
    assert _finite_return(new, 1) == pytest.approx(new["outcomes"]["1"]["executable_candidate_side_net_return_bps"])
    legacy = {"outcomes": {"1": {"status": "VALID", "candidate_side_net_return_bps": 9.0}}}
    assert _finite_return(legacy, 1) == 9.0
    unavailable = {"outcomes": {"1": {"status": "VALID", "entry_timing": "NEXT_OPEN_UNAVAILABLE",
                                    "candidate_side_net_return_bps": 99.0}}}
    assert _finite_return(unavailable, 1) is None
    missing_executable = {"outcomes": {"1": {"status": "VALID", "entry_timing": "NEXT_OPEN",
                                           "candidate_side_net_return_bps": 99.0}}}
    assert _finite_return(missing_executable, 1) is None
