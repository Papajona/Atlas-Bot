import numpy as np
import pandas as pd
from collections.abc import Mapping
from app.strategy_engine import StrategyConfig, strategy_signals, strategy_signal, strategy_backtest

def sample_df(n=700):
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.default_rng(7)
    rets = 0.00015 + rng.normal(0, 0.002, n)
    close = 100 * np.exp(np.cumsum(rets))
    open_ = close * (1 + rng.normal(0, 0.0005, n))
    # Synthetic OHLC must be structurally valid: high/low envelop both open and close.
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0015, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0015, n)))
    volume = rng.lognormal(10, 0.2, n)
    return pd.DataFrame({"open":open_, "high":high, "low":low, "close":close, "volume":volume}, index=idx)

def test_research_trial_report_separates_dsr_comparable_and_all_recorded_trials():
    from app.research_engine import _trial_accounting_fields

    fields = _trial_accounting_fields(
        known_prior_labels={"prior-trend@sha256:a", "ai@prior-model"},
        current_fixed_signal_labels={"trend@sha256:b", "momentum@sha256:c"},
        current_ai_labels={"ai@current-model"},
    )

    assert fields["trial_labels"] == [
        "ai@current-model",
        "ai@prior-model",
        "momentum@sha256:c",
        "prior-trend@sha256:a",
        "trend@sha256:b",
    ]
    assert fields["fixed_signal_trial_labels"] == [
        "momentum@sha256:c",
        "prior-trend@sha256:a",
        "trend@sha256:b",
    ]
    assert fields["ai_trial_labels"] == ["ai@current-model", "ai@prior-model"]
    assert fields["cumulative_trials"] == 5
    assert fields["cumulative_recorded_trials"] == 5
    assert fields["cumulative_fixed_signal_trials"] == 3
    assert fields["dsr_comparable_trial_count"] is None
    assert fields["cumulative_ai_trials"] == 2


def test_dsr_comparable_trial_count_uses_only_verified_archive_sharpes():
    from app.research_engine import _verified_dsr_comparable_trial_count

    # Four verified same-sample outcomes, despite only three known fixed-signal labels.
    assert _verified_dsr_comparable_trial_count(True, [0.2, 0.4, -0.1, 0.3]) == 4
    assert _verified_dsr_comparable_trial_count(False, [0.2, 0.4, -0.1, 0.3]) is None
    assert _verified_dsr_comparable_trial_count(True, [0.2]) is None
    assert _verified_dsr_comparable_trial_count(True, [0.2, float("nan")]) is None


def test_strategy_schema_and_bounds():
    df = sample_df()
    s = strategy_signals(df)
    assert {"trend","momentum","breakout","mean_reversion","ensemble","atr","realized_vol"} <= set(s.columns)
    assert float(s["ensemble"].dropna().abs().max()) <= 1.000001

def test_strategy_signal_has_protective_levels():
    df = sample_df()
    result = strategy_signal(df, StrategyConfig(signal_threshold=0.0))
    assert result["side"] in {"buy", "sell", "flat"}
    if result["side"] != "flat":
        assert result["stop_loss_price"] is not None
        assert result["take_profit_price"] is not None

def test_strategy_backtest_is_lagged_and_finite():
    result = strategy_backtest(sample_df())
    for key in ("total_return","max_drawdown","sharpe","average_leverage","max_leverage"):
        assert np.isfinite(result[key])
    assert result["max_leverage"] <= 1.500001


def test_next_open_ratio_uses_neutral_gross_multiple_on_terminal_bar():
    from app.strategy_engine import next_open_returns

    idx = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    frame = pd.DataFrame({"open": [100.0, 101.0, 102.0]}, index=idx)
    gross_multiple = next_open_returns(frame)
    net_return = gross_multiple - 1.0

    assert np.isclose(gross_multiple.iloc[0], 1.01)
    assert np.isclose(gross_multiple.iloc[1], 102.0 / 101.0)
    assert np.isclose(gross_multiple.iloc[-1], 1.0)
    assert np.isclose(net_return.iloc[-1], 0.0)



def test_strategy_annualization_uses_bar_frequency():
    import numpy as np, pandas as pd
    from app.strategy_engine import strategy_signals, strategy_backtest
    idx = pd.date_range("2025-01-01", periods=300, freq="4h", tz="UTC")
    base = np.linspace(100, 120, len(idx))
    df = pd.DataFrame({"open":base, "high":base+1, "low":base-1, "close":base}, index=idx)
    out = strategy_signals(df)
    assert 2000 < out.attrs["annualization_factor"] < 2300
    result = strategy_backtest(df)
    assert result["annualization_factor"] == out.attrs["annualization_factor"]


def test_all_strategy_research_and_ai_review():
    from app.research_engine import backtest_all_strategies, ai_market_review
    df = sample_df(1800)
    df.attrs["data_provenance"] = {
        "source": "synthetic_test",
        "symbol": "TEST",
        "exchange": "synthetic",
        "timeframe": "1h",
        "fetched_at_utc": "2026-01-01T00:00:00+00:00",
        "row_count": len(df),
        "sha256": "0" * 64,
    }
    # A persisted label is not a historical Sharpe distribution: the DSR gate must
    # remain closed until trusted prior-trial outcomes on a comparable OOS sample exist.
    out = backtest_all_strategies(df, include_ai=False, prior_trial_labels={"prior-variant@sha256:trusted-config"})
    assert out["validation"]["trial_registry_complete"] is False
    assert out["validation"]["deflated_sharpe"]["status"] == "MISSING_COMPARABLE_TRIAL_PERFORMANCE_HISTORY"
    assert out["strategy_count"] == 5
    assert set(out["research_order"]) == {"trend", "momentum", "breakout", "mean_reversion", "ensemble"}
    review = ai_market_review(out)
    assert review["status"] == "REVIEW_READY"
    assert review["research_order"]


def test_paper_gate_and_live_shadow_signals():
    from app.research_engine import paper_candidates, live_strategy_signals
    df = sample_df(1800)
    passing_folds = [{"total_return":0.05},{"total_return":0.04},{"total_return":-0.01},{"total_return":0.03},{"total_return":0.02}]
    research = {
        "asset_class": "forex",
        "universe_bias": "NOT_APPLICABLE",
        "strategies": [{"strategy":"trend","sharpe":1.0,"max_drawdown":-0.10,"trades":150,"total_return":0.20}],
        "validation": {
            "trial_registry_complete": True,
            "deflated_sharpe": {"status":"OK","deflated_sharpe_ratio":0.99},
            "per_strategy_oos": {"trend": {
                "status":"OK","oos_total_return":0.10,"sharpe":0.9,"max_drawdown":-0.10,"trades":150,
                "deflated_sharpe":0.99,"cost_stress_ok":True,"financing_cost_model_complete":True,
                "intrabar_stop_target_model_complete":True,"execution_cost_model_complete":True,"execution_cost_calibration_status":"VERIFIED","execution_cost_calibration_evidence_id":"fixture:venue-cost-calibration",
                "trial_registry_complete":True,"folds":passing_folds,
            }},
        },
    }
    candidates = paper_candidates(research)
    # The trusted signed comparable-trial archive verifier is intentionally not
    # integrated yet. Synthetic report fields must not bypass that fail-closed gate.
    assert candidates == []
    names = [x["strategy"] for x in candidates]
    signals = live_strategy_signals(df, names)
    assert signals == []

def test_multi_timeframe_structure_and_risk_levels():
    from app.multi_timeframe import multi_timeframe_analysis, multi_timeframe_signal
    df = sample_df(2200)
    out = multi_timeframe_analysis(df)
    required = {"regime_4h", "regime_1h", "setup_15m", "confirm_5m", "entry_confirmed", "entry_price", "stop_price", "take_profit_price"}
    assert required <= set(out.columns)
    sig = multi_timeframe_signal(df)
    assert sig["execution_mode"] == "PAPER_SHADOW_ONLY"
    assert sig["side"] in {"buy", "sell", "flat"}


def _install_fast_walk_forward_model(monkeypatch):
    """Test fold accounting without fitting hundreds of LightGBM trees."""
    from app import trading_core

    class DeterministicModel:
        def __init__(self, **kwargs):
            self.classes_ = np.array([0, 1, 2], dtype=int)

        def fit(self, features, labels):
            assert len(features) == len(labels)
            return self

        def predict_proba(self, features):
            # Stable long signal exercises position lag, turnover, and fold exit costs.
            return np.tile(np.array([[0.05, 0.10, 0.85]], dtype=float), (len(features), 1))

    monkeypatch.setattr(trading_core, "LGBMClassifier", DeterministicModel)


def test_ai_walk_forward_does_not_forward_fill_between_oos_windows(monkeypatch):
    from app.trading_core import ai_walk_forward_backtest

    _install_fast_walk_forward_model(monkeypatch)
    out = ai_walk_forward_backtest(sample_df(1800), asset="crypto", folds=4, min_train=500)
    expected_oos = sum(int(fold["test_bars"]) for fold in out["folds"])
    assert out["prediction_gap_policy"] == "NO_FORWARD_FILL_ACROSS_NON_TEST_WINDOWS"
    assert out["oos_bars"] == expected_oos
    assert out["validated_bars"] == expected_oos
    assert out["validation_signal_count"] == expected_oos
    assert 0 < expected_oos < out["bars"]
    assert np.isfinite(out["sharpe"])


def test_ai_walk_forward_aggregate_metrics_only_use_held_out_bars(monkeypatch):
    from app import trading_core

    _install_fast_walk_forward_model(monkeypatch)

    captured = {}
    original_cost_report = trading_core.cost_breakeven_report

    def cost_report_spy(returns_gross, turnover, **kwargs):
        captured["cost_return_count"] = len(returns_gross)
        captured["cost_turnover_count"] = len(turnover)
        captured["fixed_cost_count"] = len(kwargs.get("fixed_cost_drag", []))
        captured["bars_per_year"] = kwargs.get("bars_per_year")
        captured["taker_bps"] = kwargs.get("taker_bps")
        captured["slippage_bps"] = kwargs.get("slippage_bps")
        return original_cost_report(returns_gross, turnover, **kwargs)

    def dsr_spy(net_returns, trial_sharpes_annual, bars_per_year):
        captured["dsr_return_count"] = len(net_returns)
        captured["dsr_bars_per_year"] = bars_per_year
        return {"status": "MISSING_TRIAL_HISTORY"}

    monkeypatch.setattr(trading_core, "cost_breakeven_report", cost_report_spy)
    monkeypatch.setattr(trading_core, "_research_trial_dsr_report", dsr_spy)

    frame = sample_df(1800)
    result = trading_core.ai_walk_forward_backtest(
        frame,
        asset="crypto",
        folds=4,
        min_train=500,
        bars_per_year=252,
        taker_bps=0.8,
        slippage_bps=1.2,
        carry_bps_per_bar=0.1,
        max_hold_bars=10,
    )
    expected_oos = sum(int(fold["test_bars"]) for fold in result["folds"])
    assert len(result["folds"]) == 4
    assert result["oos_bars"] == expected_oos
    assert 0 < expected_oos < result["bars"]
    # OHLC-based stop/target simulation cannot prove the true intrabar event order.
    assert result["intrabar_stop_target_model_complete"] is False
    assert result["execution_stop_target_audit"]["ambiguous_bars_filled_stop_first"] >= 0
    assert captured == {
        "cost_return_count": expected_oos,
        "cost_turnover_count": expected_oos,
        "fixed_cost_count": expected_oos,
        "bars_per_year": 252,
        "taker_bps": 0.8,
        "slippage_bps": 1.2,
        "dsr_return_count": expected_oos,
        "dsr_bars_per_year": 252,
    }
    assert result["research_assumptions"] == {
        "asset_profile": "crypto",
        "bars_per_year": 252,
        "taker_bps": 0.8,
        "slippage_bps": 1.2,
        "carry_bps_per_bar": 0.1,
        "carry_rate_configured": True,
        "carry_evidence_source": None,
        "carry_evidence_id": None,
        "carry_evidence_complete": False,
        "max_hold_bars": 10,
        "stop_atr": trading_core.settings.strategy_stop_atr,
        "take_profit_atr": trading_core.settings.strategy_take_profit_atr,
    }
    assert result["research_trial_history_status"] == "MISSING_TRIAL_HISTORY"


def test_adaptive_policy_object_is_explicit_and_fail_closed():
    from app.adaptive_bot import AdaptiveModelPolicy
    p = AdaptiveModelPolicy()
    assert p.max_age_hours > 0
    assert p.min_sharpe == 0.50
    assert p.max_drawdown == -0.20
    assert p.min_trades == 100
    assert p.research_min_deflated_sharpe == 0.95
    assert p.research_require_cost_stress is True


def test_adaptive_champion_promotion_keeps_model_integrity(tmp_path, monkeypatch):
    from app import adaptive_bot, model_registry
    import hashlib, json, base64
    import pathlib
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    model_path = tmp_path / "fx.joblib"
    candidate_result = {"model_sha256": "abc123", "features": 20, "folds": 3}

    signing_key = Ed25519PrivateKey.generate()
    public_pem = signing_key.public_key().public_bytes(
        Encoding.PEM,
        PublicFormat.SubjectPublicKeyInfo,
    )
    monkeypatch.setattr(model_registry.settings, "environment", "staging", raising=False)
    monkeypatch.setattr(model_registry.settings, "model_integrity_required_for_live", True, raising=False)
    monkeypatch.setattr(model_registry.settings, "model_signing_public_key", public_pem.decode(), raising=False)
    wfo = {"sharpe": 1.0, "max_drawdown": -0.10, "trades": 150, "total_return": 0.20,
           "folds": [{"total_return": 0.03}, {"total_return": 0.02}, {"total_return": 0.04}, {"total_return": 0.05}, {"total_return": 0.06}],
           "cost_stress_ok": True,
        "financing_cost_model_complete": True,
        "intrabar_stop_target_model_complete": True,
        "execution_cost_model_complete": True,
        "slippage_model_complete": True,
        "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_evidence_id": "fixture:venue-cost-calibration", "status": "OK", "oos_total_return": 0.20, "deflated_sharpe": 0.97,
           "trial_registry_complete": True,
           "inference": {"status": "OK", "newey_west_t_statistic": 2.0, "effective_observations": 200},
           "benchmark_comparison": {"status": "OK", "gate_passed": True},
           "trade_boundary_overlap_bars": 0,
           "financing_cost_model_complete": True, "intrabar_stop_target_model_complete": True}

    monkeypatch.setattr(adaptive_bot.settings, "research_carry_bps_per_bar", 0.1)
    monkeypatch.setattr(adaptive_bot.settings, "research_carry_evidence_source", "fixture:venue-financing")
    monkeypatch.setattr(adaptive_bot.settings, "research_carry_evidence_id", "fixture-rate-sheet-v1")
    monkeypatch.setattr(adaptive_bot, "ai_walk_forward_backtest", lambda *a, **k: wfo)
    def fake_train(df, path, **kwargs):
        pathlib.Path(path).write_bytes(b"model-bytes")
        digest = hashlib.sha256(b"model-bytes").hexdigest()
        signature = base64.urlsafe_b64encode(
            signing_key.sign(digest.encode())
        ).decode().rstrip("=")
        pathlib.Path(str(path) + ".meta.json").write_text(
            json.dumps({
                "model_sha256": digest,
                "manifest_signature": signature,
            })
        )
        return {**candidate_result, "model_sha256": digest}
    monkeypatch.setattr(adaptive_bot, "train_model", fake_train)

    out = adaptive_bot.ensure_adaptive_model(object(), str(model_path), asset="forex", policy=adaptive_bot.AdaptiveModelPolicy(min_trades=1))
    assert out["status"] == "CHALLENGER_PASSED_AWAITING_CONFIRMATION"  # consecutive-pass gate: first pass doesn't promote yet
    assert not model_path.exists()
    streak = pathlib.Path(str(model_path) + ".candidate_streak.json")
    value = json.loads(streak.read_text())
    value["evaluated_at"] = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=21)).isoformat()
    streak.write_text(json.dumps(value))
    out = adaptive_bot.ensure_adaptive_model(object(), str(model_path), asset="forex", policy=adaptive_bot.AdaptiveModelPolicy(min_trades=1))
    assert out["status"] == "CHAMPION_PROMOTED"
    assert model_path.exists()
    assert json.loads(pathlib.Path(str(model_path)+".meta.json").read_text())["model_sha256"] == hashlib.sha256(b"model-bytes").hexdigest()
    assert json.loads(pathlib.Path(str(model_path)+".adaptive.json").read_text())["status"] == "CHAMPION"

def test_strategy_backtest_reports_next_open_execution_model():
    result = strategy_backtest(sample_df(), taker_bps=0, slippage_bps=0)
    assert result["backtest_model"].startswith("next-open entries, OHLC intrabar ATR stop/target fills")


def test_ai_walk_forward_trial_registry_requires_verified_archive(monkeypatch):
    from app import trading_core

    _install_fast_walk_forward_model(monkeypatch)
    monkeypatch.setattr(
        trading_core,
        "_research_trial_dsr_report",
        lambda *args, **kwargs: {"status": "OK", "deflated_sharpe_ratio": 0.97},
    )
    frame = sample_df(1800)
    variants = [0.2, 0.3, -0.1]

    unverified = trading_core.ai_walk_forward_backtest(
        frame, asset="crypto", folds=3, min_train=500,
        research_trial_sharpes=variants, bars_per_year=252,
        carry_bps_per_bar=0.1,
    )
    assert unverified["deflated_sharpe_status"] == "OK"
    assert unverified["trial_registry_complete"] is False
    assert unverified["trial_registry_evidence_verified"] is False

    verified = trading_core.ai_walk_forward_backtest(
        frame, asset="crypto", folds=3, min_train=500,
        research_trial_sharpes=variants, bars_per_year=252,
        carry_bps_per_bar=0.1, research_trial_evidence_verified=True,
    )
    assert verified["trial_registry_complete"] is True
    assert verified["trial_registry_evidence_verified"] is True


def test_staging_adaptive_model_fails_closed_without_carry_evidence(tmp_path, monkeypatch):
    from app import adaptive_bot

    model_path = tmp_path / "model.joblib"
    monkeypatch.setattr(adaptive_bot.settings, "environment", "staging", raising=False)
    monkeypatch.setattr(adaptive_bot.settings, "research_carry_bps_per_bar", None, raising=False)
    monkeypatch.setattr(adaptive_bot.settings, "research_carry_evidence_source", "", raising=False)
    monkeypatch.setattr(adaptive_bot.settings, "research_carry_evidence_id", "", raising=False)

    def unexpected_walk_forward(*args, **kwargs):
        raise AssertionError("walk-forward/promotion must not run without carry evidence")

    monkeypatch.setattr(adaptive_bot, "ai_walk_forward_backtest", unexpected_walk_forward)
    result = adaptive_bot.ensure_adaptive_model(sample_df(900), str(model_path))
    assert result["status"] == "VALIDATION_UNAVAILABLE"
    assert result["financing_cost_model_complete"] is False
    assert not model_path.exists()


def test_paper_candidates_use_oos_metrics_after_signed_archive_verification(monkeypatch):
    # The cryptographic archive contract is tested in test_research_evidence.py.
    # This unit test covers the downstream selector's use of the validator result.
    from app.research_engine import paper_candidates
    from app.config import settings

    monkeypatch.setattr(settings, "research_folds", 5)
    research = {
        "asset_class": "forex",
        "universe_bias": "NOT_APPLICABLE",
        "strategies": [{
            "strategy": "trend", "sharpe": -99.0, "max_drawdown": -0.99,
            "trades": 0, "total_return": -9.0,
        }],
        "validation": {
            "walk_forward_purge_complete": True,
            "trial_registry_complete": True,
            "deflated_sharpe": {
                "status": "OK", "deflated_sharpe_ratio": 0.99,
                "trial_scope": "VERIFIED_ARCHIVE_SAME_OOS_SAMPLE",
                "trial_count_current_run": 5, "trial_count_archive": 5,
            },
            "comparable_trial_archive": {
                "verified": True, "trial_archive_verified": True,
                "evidence_id": "test-signed-archive",
                "evidence_sha256": "a" * 64,
            },
            "per_strategy_oos": {"trend": {
                "status": "OK", "oos_total_return": 0.10, "sharpe": 0.9,
                "max_drawdown": -0.10, "trades": 150, "deflated_sharpe": 0.99,
                "cost_stress_ok": True, "financing_cost_model_complete": True,
                "intrabar_stop_target_model_complete": True,
                "execution_cost_model_complete": True,
                "slippage_model_complete": True,
                "execution_cost_calibration_status": "VERIFIED",
                "execution_cost_calibration_evidence_id": "test-cost-evidence",
                "trial_registry_complete": True,
                "trade_boundary_overlap_bars": 0,
                "inference": {"status": "OK", "newey_west_t_statistic": 2.0, "effective_observations": 150},
                "benchmark_comparison": {"status": "OK", "gate_passed": True},
                "folds": [
                    {"total_return": 0.05}, {"total_return": 0.04},
                    {"total_return": -0.01}, {"total_return": 0.03},
                    {"total_return": 0.02},
                ],
            }},
        },
    }

    candidates = paper_candidates(research)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["selection_basis"] == "OUT_OF_SAMPLE_ONLY"
    assert candidate["metrics"]["metric_basis"] == "OUT_OF_SAMPLE"
    assert candidate["metrics"]["sharpe"] == 0.9
    assert candidate["metrics"]["total_return"] == 0.10
    assert candidate["in_sample_diagnostics"]["sharpe"] == -99.0

def test_multi_timeframe_resampling_uses_open_timestamp_buckets_without_boundary_shift():
    """Each source candle belongs to the higher-timeframe interval in which it opens."""
    from app.multi_timeframe import _resample_ohlcv

    idx = pd.date_range("2025-01-01 00:00:00", periods=96, freq="5min", tz="UTC")
    open_ = 100.0 + np.arange(len(idx), dtype=float)
    df = pd.DataFrame(
        {
            "open": open_,
            "high": open_ + 2.0,
            "low": open_ - 3.0,
            "close": open_ + 0.25,
            "volume": np.ones(len(idx), dtype=float),
        },
        index=idx,
    )

    bars = _resample_ohlcv(df, "4h")

    assert bars.index.tolist() == [
        pd.Timestamp("2025-01-01 04:00:00", tz="UTC"),
        pd.Timestamp("2025-01-01 08:00:00", tz="UTC"),
    ]
    # [00:00, 04:00): source rows at 00:00 through 03:55, exactly 48 5-minute bars.
    assert bars.iloc[0]["open"] == 100.0
    assert bars.iloc[0]["high"] == 149.0
    assert bars.iloc[0]["low"] == 97.0
    assert bars.iloc[0]["close"] == 147.25
    assert bars.iloc[0]["volume"] == 48.0
    # The 04:00 source candle starts the next interval; it is not folded into the prior bar.
    assert bars.iloc[1]["open"] == 148.0
    assert bars.iloc[1]["high"] == 197.0
    assert bars.iloc[1]["low"] == 145.0
    assert bars.iloc[1]["close"] == 195.25
    assert bars.iloc[1]["volume"] == 48.0




def test_triple_barrier_prices_label_entry_and_exit_with_per_bar_execution_evidence(monkeypatch):
    from app import research_execution_costs
    from app.trading_core import triple_barrier

    index = pd.date_range("2026-01-01", periods=6, freq="h", tz="UTC")
    close = pd.Series([100.0] * len(index), index=index)
    opened = pd.Series([100.0] * len(index), index=index)
    high = pd.Series([101.0] * len(index), index=index)
    low = pd.Series([99.0] * len(index), index=index)
    atr = pd.Series([1.0] * len(index), index=index)
    context = {
        "index": index,
        "spread_bps": np.ones(len(index)),
        "volume_units": np.full(len(index), 100_000.0),
        "volatility_bps": np.full(len(index), 10.0),
        "model_fingerprint": "a" * 64,
        "context_sha256": "b" * 64,
    }
    priced_events = []

    def price_event(_context, **kwargs):
        priced_events.append(kwargs)
        return 0.25, None

    monkeypatch.setattr(
        research_execution_costs, "execution_cost_for_event", price_event
    )
    labels = triple_barrier(
        close,
        pd.Series([0.01] * len(index), index=index),
        high,
        low,
        max_hold=2,
        entry_open=opened,
        entry_atr=atr,
        stop_atr=5.0,
        take_profit_atr=5.0,
        execution_cost_context=context,
        execution_cost_namespace="test-ai-labels",
    )

    assert labels.iloc[: len(index) - 2].notna().all()
    assert labels.attrs["execution_cost_model_complete"] is True
    assert labels.attrs["execution_cost_model_fingerprint"] == "a" * 64
    assert labels.attrs["per_bar_execution_context_sha256"] == "b" * 64
    assert labels.attrs["execution_cost_label_event_pairs_priced"] > 0
    assert {event["side"] for event in priced_events} == {"buy", "sell"}
    assert {event["namespace"] for event in priced_events} == {
        "test-ai-labels:ENTRY",
        "test-ai-labels:EXIT",
    }
    assert all(event["bar_position"] > 0 for event in priced_events)



def _validator_issued_ai_execution_context(frame, *, model_fingerprint, context_sha256):
    from app.research_execution_costs import (
        _VERIFIED_RESEARCH_EXECUTION_CONTEXT_TOKEN,
        _VerifiedResearchExecutionCostContext,
    )

    return _VerifiedResearchExecutionCostContext(
        {
            "index": frame.index,
            "spread_bps": np.full(len(frame), 1.5),
            "volume_units": np.full(len(frame), 100_000.0),
            "volatility_bps": np.full(len(frame), 20.0),
            "model_fingerprint": model_fingerprint,
            "context_sha256": context_sha256,
            "evidence_id": "fixture:verified-calibration",
            "execution_cost_calibration_evidence_id": "fixture:verified-calibration",
            "execution_cost_calibration_status": "VERIFIED",
        },
        _token=_VERIFIED_RESEARCH_EXECUTION_CONTEXT_TOKEN,
    )


def test_ai_walk_forward_applies_verified_execution_context_to_labels_folds_and_benchmarks(monkeypatch):
    from app import research_execution_costs, trading_core

    _install_fast_walk_forward_model(monkeypatch)
    frame = sample_df(1800)
    context = _validator_issued_ai_execution_context(
        frame, model_fingerprint="c" * 64, context_sha256="d" * 64
    )
    seen = {"provider_namespaces": [], "provider_indices": []}

    def labels_with_verified_costs(close, vol, high, low, **kwargs):
        label_context = kwargs.get("execution_cost_context")
        assert label_context is not None
        assert label_context["index"].equals(close.index)
        seen["label_namespace"] = kwargs.get("execution_cost_namespace")
        result = pd.Series(
            np.resize(np.array([-1.0, 0.0, 1.0]), len(close)),
            index=close.index,
            dtype=float,
        )
        result.attrs["execution_cost_model_complete"] = True
        result.attrs["execution_cost_model_status"] = "VERIFIED_PER_BAR_SPREAD_VOLUME_PARTICIPATION_LATENCY"
        result.attrs["execution_cost_failure_reasons"] = {}
        result.attrs["execution_cost_label_event_pairs_priced"] = len(close)
        return result

    def provider_for_fold(fold_context, *, namespace):
        seen["provider_namespaces"].append(namespace)
        seen["provider_indices"].append(fold_context["index"])
        return lambda *_args: 0.25

    def benchmark_spy(*_args, **kwargs):
        contexts = kwargs.get("execution_cost_context_blocks")
        seen["benchmark_context_count"] = len(contexts or [])
        seen["benchmark_namespace"] = kwargs.get("candidate_execution_cost_namespace")
        return {
            "status": "OK",
            "execution_cost_model_complete": bool(contexts)
            and all(isinstance(item, Mapping) for item in contexts),
        }

    monkeypatch.setattr(trading_core, "triple_barrier", labels_with_verified_costs)
    monkeypatch.setattr(
        research_execution_costs, "make_execution_cost_provider", provider_for_fold
    )
    monkeypatch.setattr(trading_core, "oos_benchmark_comparison", benchmark_spy)

    result = trading_core.ai_walk_forward_backtest(
        frame,
        asset="crypto",
        folds=4,
        min_train=500,
        execution_cost_context=context,
    )

    assert seen["label_namespace"] == "ai-walk-forward"
    assert seen["provider_namespaces"] == ["ai-walk-forward"] * 4
    assert len(seen["provider_indices"]) == 4
    assert seen["benchmark_context_count"] == 4
    assert seen["benchmark_namespace"] == "ai-walk-forward"
    assert result["execution_cost_model_complete"] is True
    assert result["execution_cost_model_status"] == (
        "VERIFIED_PER_BAR_SPREAD_VOLUME_PARTICIPATION_LATENCY"
    )
    assert result["execution_cost_model_fingerprint"] == "c" * 64
    assert result["per_bar_execution_context_sha256"] == "d" * 64
    assert result["execution_cost_calibration_evidence_id"] == (
        "fixture:verified-calibration"
    )


def test_ai_walk_forward_falls_back_for_whole_fold_after_calibrated_event_rejection(monkeypatch):
    from app import research_execution_costs, trading_core

    _install_fast_walk_forward_model(monkeypatch)
    frame = sample_df(1800)
    context = _validator_issued_ai_execution_context(
        frame, model_fingerprint="e" * 64, context_sha256="f" * 64
    )

    def labels_with_verified_costs(close, vol, high, low, **kwargs):
        result = pd.Series(
            np.resize(np.array([-1.0, 0.0, 1.0]), len(close)),
            index=close.index,
            dtype=float,
        )
        result.attrs["execution_cost_model_complete"] = True
        result.attrs["execution_cost_failure_reasons"] = {}
        result.attrs["execution_cost_label_event_pairs_priced"] = len(close)
        return result

    provider_count = {"value": 0}

    def rejected_provider(_fold_context, *, namespace):
        provider_count["value"] += 1
        if provider_count["value"] == 1:
            def reject_event(*_args):
                raise research_execution_costs.ResearchExecutionCostError(
                    "ORDER_PARTICIPATION_LIMIT_EXCEEDED"
                )
            return reject_event
        return lambda *_args: 0.25

    def benchmark_spy(*_args, **kwargs):
        contexts = kwargs.get("execution_cost_context_blocks")
        assert isinstance(contexts, list)
        assert len(contexts) == 4
        assert contexts[0] is None
        assert all(isinstance(item, Mapping) for item in contexts[1:])
        return {"status": "OK", "execution_cost_model_complete": False}

    monkeypatch.setattr(trading_core, "triple_barrier", labels_with_verified_costs)
    monkeypatch.setattr(
        research_execution_costs, "make_execution_cost_provider", rejected_provider
    )
    monkeypatch.setattr(trading_core, "oos_benchmark_comparison", benchmark_spy)

    result = trading_core.ai_walk_forward_backtest(
        frame,
        asset="crypto",
        folds=4,
        min_train=500,
        execution_cost_context=context,
    )

    assert result["execution_cost_model_complete"] is False
    assert result["execution_cost_model_status"] == (
        "INCOMPLETE_AI_OOS_FOLD_EXECUTION_COSTS"
    )
    assert (
        result["execution_cost_failure_reasons"]["ORDER_PARTICIPATION_LIMIT_EXCEEDED"]
        == 1
    )
    assert [fold["execution_cost_model_complete"] for fold in result["folds"]] == [
        False, True, True, True
    ]



def test_ai_walk_forward_keeps_unpriced_labels_on_original_oos_timeline(monkeypatch):
    from app import trading_core
    from app.research_validation import purged_walk_forward_splits

    _install_fast_walk_forward_model(monkeypatch)
    frame = sample_df(1800)
    features = trading_core.build_features(frame)
    folds, min_train, hold_horizon = 4, 500, trading_core.PROFILES["crypto"].max_hold
    test_size = (
        len(features) - min_train - (2 * folds - 1) * hold_horizon
    ) // folds
    split_specs = purged_walk_forward_splits(
        len(features),
        folds=folds,
        min_train=min_train,
        test_size=test_size,
        purge=hold_horizon,
        embargo=hold_horizon,
    )
    unpriced_index = features.index[split_specs[1].test_start + 5]
    captured = []
    original_simulator = trading_core.simulate_execution_aware_strategy

    def labels_with_one_unpriced_bar(close, vol, high, low, **kwargs):
        labels = pd.Series(
            np.resize(np.array([-1.0, 0.0, 1.0]), len(close)),
            index=close.index,
            dtype=float,
        )
        labels.loc[unpriced_index] = np.nan
        labels.attrs["execution_cost_model_complete"] = False
        labels.attrs["execution_cost_model_status"] = "INCOMPLETE_PER_BAR_LABEL_COSTS"
        labels.attrs["execution_cost_failure_reasons"] = {
            "SYNTHETIC_UNPRICED_LABEL": 1
        }
        labels.attrs["execution_cost_label_event_pairs_priced"] = 0
        return labels

    def capture_simulation_bars(fold_frame, *args, **kwargs):
        captured.append(fold_frame.index.copy())
        return original_simulator(fold_frame, *args, **kwargs)

    monkeypatch.setattr(trading_core, "triple_barrier", labels_with_one_unpriced_bar)
    monkeypatch.setattr(
        trading_core, "simulate_execution_aware_strategy", capture_simulation_bars
    )
    monkeypatch.setattr(
        trading_core,
        "oos_benchmark_comparison",
        lambda *_args, **_kwargs: {
            "status": "OK",
            "execution_cost_model_complete": False,
        },
    )

    result = trading_core.ai_walk_forward_backtest(
        frame, asset="crypto", folds=folds, min_train=min_train
    )

    assert len(captured) == folds
    assert len(result["folds"]) == folds
    for i, split in enumerate(split_specs):
        expected_index = features.index[split.test_start:split.test_end]
        assert captured[i].equals(expected_index)
        assert captured[i].is_monotonic_increasing
        assert captured[i].to_series().diff().dropna().eq(pd.Timedelta(hours=1)).all()
    assert unpriced_index in captured[1]
    assert result["oos_bars"] == sum(len(index) for index in captured)
    assert result["validated_bars"] == result["oos_bars"]
    assert result["execution_cost_model_complete"] is False


def test_triple_barrier_timeout_counts_unpriceable_label_once(monkeypatch):
    from app import research_execution_costs
    from app.trading_core import triple_barrier

    index = pd.date_range("2026-01-01", periods=6, freq="h", tz="UTC")
    close = pd.Series([100.0] * len(index), index=index)
    opened = pd.Series([100.0] * len(index), index=index)
    high = pd.Series([101.0] * len(index), index=index)
    low = pd.Series([99.0] * len(index), index=index)
    atr = pd.Series([1.0] * len(index), index=index)
    context = {
        "index": index,
        "spread_bps": np.ones(len(index)),
        "volume_units": np.full(len(index), 100_000.0),
        "volatility_bps": np.full(len(index), 10.0),
        "model_fingerprint": "1" * 64,
        "context_sha256": "2" * 64,
    }
    calls = []

    def reject_event(_context, **kwargs):
        calls.append(kwargs)
        return None, "SIMULATED_COST_REJECTION"

    monkeypatch.setattr(
        research_execution_costs, "execution_cost_for_event", reject_event
    )
    labels = triple_barrier(
        close,
        pd.Series([0.01] * len(index), index=index),
        high,
        low,
        max_hold=2,
        entry_open=opened,
        entry_atr=atr,
        stop_atr=10.0,
        take_profit_atr=10.0,
        execution_cost_context=context,
    )

    assert labels.iloc[:4].isna().all()
    assert labels.attrs["execution_cost_failure_reasons"] == {
        "SIMULATED_COST_REJECTION": 4
    }
    assert labels.attrs["execution_cost_label_event_pairs_priced"] == 0
    # One rejected label is priced once, not once again for its opposite direction.
    assert len(calls) == 4
