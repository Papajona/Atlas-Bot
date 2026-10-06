"""ensure_adaptive_model() previously promoted a challenger to live-eligible CHAMPION the
first time its walk-forward gate passed -- one good daily cycle was enough. These tests
cover the new consecutive-pass requirement: a challenger must clear the same gate on
`policy.consecutive_passes_required` consecutive evaluation cycles (tracked in a streak
file, reset by any failure) before it is actually promoted."""
import json

import numpy as np
import pandas as pd
import pytest

import app.adaptive_bot as adaptive_bot
from app.adaptive_bot import AdaptiveModelPolicy, ensure_adaptive_model, _streak_path


def _synthetic_ohlcv(n=900, seed=11):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0003, 0.008, n)
    close = 100 * np.cumprod(1 + ret)
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    high = close * (1 + np.abs(rng.normal(0, 0.002, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.002, n)))
    openp = np.roll(close, 1); openp[0] = close[0]
    vol = rng.uniform(100, 1000, n)
    return pd.DataFrame({"open": openp, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def _passing_wfo():
    folds = [{"total_return": 0.05}, {"total_return": 0.04}, {"total_return": 0.03},
              {"total_return": 0.02}, {"total_return": 0.06}]
    return {"sharpe": 1.2, "max_drawdown": -0.05, "trades": 50, "total_return": 0.10,
            "folds": folds, "cost_stress_ok": True, "status": "OK", "oos_total_return": 0.10, "deflated_sharpe": 1.0}


def _failing_wfo():
    folds = [{"total_return": -0.05}, {"total_return": -0.04}, {"total_return": 0.01},
              {"total_return": -0.02}, {"total_return": -0.06}]
    return {"sharpe": -0.5, "max_drawdown": -0.40, "trades": 50, "total_return": -0.10, "folds": folds}


@pytest.fixture
def mocked_training(monkeypatch, tmp_path):
    """Avoid real model training/sklearn work -- these tests are about the promotion
    bookkeeping, not the training pipeline (covered elsewhere)."""
    def fake_train_model(df, model_path, min_train=800, folds=5, asset="crypto"):
        with open(model_path, "wb") as fh:
            fh.write(b"fake-model-bytes")
        meta_path = str(model_path) + ".meta.json"
        with open(meta_path, "w") as fh:
            json.dump({"ok": True}, fh)
        return {"model_sha256": "deadbeef", "features": 12}

    monkeypatch.setattr(adaptive_bot, "train_model", fake_train_model)
    monkeypatch.setattr(adaptive_bot, "verify_model_file", lambda *a, **k: True)
    return tmp_path


def test_single_pass_does_not_promote_by_default(mocked_training):
    model_path = mocked_training / "model.pkl"
    monkeypatch_wfo = _passing_wfo()
    import app.adaptive_bot as ab
    orig = ab.ai_walk_forward_backtest
    ab.ai_walk_forward_backtest = lambda *a, **k: monkeypatch_wfo
    try:
        result = ensure_adaptive_model(_synthetic_ohlcv(), str(model_path), policy=AdaptiveModelPolicy())
        assert result["status"] == "CHALLENGER_PASSED_AWAITING_CONFIRMATION"
        assert result["consecutive_passes"] == 1
        assert result["consecutive_passes_required"] == 2
        assert not model_path.exists()  # must NOT have promoted yet
    finally:
        ab.ai_walk_forward_backtest = orig


def test_two_consecutive_passes_promotes_with_default_policy(mocked_training):
    model_path = mocked_training / "model.pkl"
    import app.adaptive_bot as ab
    orig = ab.ai_walk_forward_backtest
    ab.ai_walk_forward_backtest = lambda *a, **k: _passing_wfo()
    try:
        first = ensure_adaptive_model(_synthetic_ohlcv(), str(model_path), policy=AdaptiveModelPolicy())
        assert first["status"] == "CHALLENGER_PASSED_AWAITING_CONFIRMATION"
        streak = _streak_path(model_path)
        value = json.loads(streak.read_text())
        value["evaluated_at"] = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=21)).isoformat()
        streak.write_text(json.dumps(value))
        second = ensure_adaptive_model(_synthetic_ohlcv(seed=12), str(model_path), policy=AdaptiveModelPolicy())
        assert second["status"] == "CHAMPION_PROMOTED"
        assert second["consecutive_passes"] == 2
        assert model_path.exists()
    finally:
        ab.ai_walk_forward_backtest = orig


def test_a_failure_resets_the_streak(mocked_training):
    model_path = mocked_training / "model.pkl"
    import app.adaptive_bot as ab
    orig = ab.ai_walk_forward_backtest
    try:
        ab.ai_walk_forward_backtest = lambda *a, **k: _passing_wfo()
        r1 = ensure_adaptive_model(_synthetic_ohlcv(seed=1), str(model_path), policy=AdaptiveModelPolicy())
        assert r1["status"] == "CHALLENGER_PASSED_AWAITING_CONFIRMATION"
        assert r1["consecutive_passes"] == 1

        ab.ai_walk_forward_backtest = lambda *a, **k: _failing_wfo()
        r2 = ensure_adaptive_model(_synthetic_ohlcv(seed=2), str(model_path), policy=AdaptiveModelPolicy())
        assert r2["status"] == "CHALLENGER_REJECTED"
        assert json.loads(_streak_path(model_path).read_text())["consecutive_passes"] == 0

        # One pass after the reset must NOT be enough -- the earlier pass doesn't carry over.
        ab.ai_walk_forward_backtest = lambda *a, **k: _passing_wfo()
        streak = _streak_path(model_path)
        value = json.loads(streak.read_text())
        value["evaluated_at"] = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=21)).isoformat()
        streak.write_text(json.dumps(value))
        r3 = ensure_adaptive_model(_synthetic_ohlcv(seed=3), str(model_path), policy=AdaptiveModelPolicy())
        assert r3["status"] == "CHALLENGER_PASSED_AWAITING_CONFIRMATION"
        assert r3["consecutive_passes"] == 1
        assert not model_path.exists()
    finally:
        ab.ai_walk_forward_backtest = orig


def test_required_count_is_configurable_toward_longer_evidence_windows(mocked_training):
    """Raising consecutive_passes_required is the lever for something closer to
    '1-2 weeks of evidence' while keeping daily retraining -- confirm it's honored."""
    model_path = mocked_training / "model.pkl"
    import app.adaptive_bot as ab
    orig = ab.ai_walk_forward_backtest
    ab.ai_walk_forward_backtest = lambda *a, **k: _passing_wfo()
    policy = AdaptiveModelPolicy(consecutive_passes_required=4)
    try:
        for i in range(3):
            r = ensure_adaptive_model(_synthetic_ohlcv(seed=20 + i), str(model_path), policy=policy)
            assert r["status"] == "CHALLENGER_PASSED_AWAITING_CONFIRMATION"
            assert r["consecutive_passes"] == i + 1
            assert not model_path.exists()
            if i < 2:
                streak = _streak_path(model_path)
                value = json.loads(streak.read_text())
                value["evaluated_at"] = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=21)).isoformat()
                streak.write_text(json.dumps(value))
        final = ensure_adaptive_model(_synthetic_ohlcv(seed=99), str(model_path), policy=policy)
        assert final["status"] == "CHAMPION_PROMOTED"
        assert final["consecutive_passes"] == 4
    finally:
        ab.ai_walk_forward_backtest = orig


def test_promotion_clears_the_streak_file_for_the_next_cycle(mocked_training):
    model_path = mocked_training / "model.pkl"
    import app.adaptive_bot as ab
    orig = ab.ai_walk_forward_backtest
    ab.ai_walk_forward_backtest = lambda *a, **k: _passing_wfo()
    try:
        ensure_adaptive_model(_synthetic_ohlcv(seed=1), str(model_path), policy=AdaptiveModelPolicy())
        streak = _streak_path(model_path)
        value = json.loads(streak.read_text())
        value["evaluated_at"] = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=21)).isoformat()
        streak.write_text(json.dumps(value))
        ensure_adaptive_model(_synthetic_ohlcv(seed=2), str(model_path), policy=AdaptiveModelPolicy())
        assert not _streak_path(model_path).exists()
    finally:
        ab.ai_walk_forward_backtest = orig


def test_consecutive_passes_required_defaults_to_two_strictly_safer_than_before():
    """Regression guard: default must stay >= 2 -- 1 would silently reinstate the old
    promote-on-first-pass behavior this feature exists to close."""
    assert AdaptiveModelPolicy().consecutive_passes_required >= 2
