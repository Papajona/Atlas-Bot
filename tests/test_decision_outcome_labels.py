from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import pytest

from app.decision_outcomes import (
    DEFAULT_HORIZONS_BARS,
    OutcomeLabelError,
    build_decision_outcome_label,
)


def _frame(*, as_of=None):
    index = pd.date_range("2026-01-01T00:00:00Z", periods=10, freq="h")
    close = np.array([100.0, 101.0, 102.0, 104.0, 103.0, 105.0, 106.0, 107.0, 108.0, 109.0])
    df = pd.DataFrame(
        {
            "open": close - 0.25,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(len(close), 1000.0),
        },
        index=index,
    )
    fetched_at = as_of if as_of is not None else (index[-1] + pd.Timedelta(hours=1))
    canonical = df.reset_index().to_csv(index=False, float_format="%.17g", lineterminator="\n")
    df.attrs["data_provenance"] = {
        "source": "ccxt_ohlcv",
        "symbol": "BTC/USDT:USDT",
        "exchange": "bybit",
        "timeframe": "1h",
        "fetched_at_utc": fetched_at.isoformat(),
        "row_count": len(df),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    return df


def _decision(timestamp=None):
    timestamp = timestamp if timestamp is not None else pd.Timestamp("2026-01-01T02:00:00Z")
    return {
        "record_type": "adaptive_trade_decision",
        "decision_id": "decision-001",
        "snapshot_sha256": "a" * 64,
        "decision": "NO_TRADE",
        "mode": "PAPER",
        "effective_mode": "NOT_EXECUTED",
        "stage": "adaptive_model_confirmation",
        "asset": "crypto",
        "symbol": "BTC/USDT:USDT",
        "exchange": "bybit",
        "timeframe": "1h",
        "market_timestamp": timestamp.isoformat(),
        "market_timestamp_source": "analysis_timestamp",
        "decision_context": {
            "analysis": {"trade_plan": {"side": "buy"}},
            "trade_plan": {},
            "strategy_router": {"strategy": "trend", "regime": "TREND_UP"},
            "decision_outcome": {"reason": "model_veto"},
        },
    }


def test_outcome_label_uses_subsequent_completed_bars_and_configured_costs():
    df = _frame()
    label = build_decision_outcome_label(_decision(), df)

    assert label["status"] == "VALID"
    assert label["research_only"] is True
    assert label["not_for_training"] is True
    assert label["automatic_promotion"] is False
    assert label["horizons_bars"] == list(DEFAULT_HORIZONS_BARS)
    assert label["candidate_side"] == "buy"
    assert label["strategy"] == "trend"
    assert label["regime"] == "TREND_UP"
    assert label["decision_reason_code"] == "model_veto"
    assert label["cost_bps_per_side"] == pytest.approx(7.5)
    one = label["outcomes"]["1"]
    assert one["baseline_bar_open"] == "2026-01-01T02:00:00+00:00"
    assert one["outcome_bar_open"] == "2026-01-01T03:00:00+00:00"
    assert one["forward_return_bps"] == pytest.approx((104.0 / 102.0 - 1.0) * 10000)
    assert one["carry_bps_per_bar"] == pytest.approx(0.125)
    assert one["carry_cost_bps"] == pytest.approx(0.125)
    assert one["candidate_side_net_return_bps"] == pytest.approx(one["forward_return_bps"] - 15.125)
    assert len(label["outcome_window_sha256"]) == 64
    assert len(label["label_id"]) == 64


def test_label_window_hash_and_outcome_do_not_depend_on_bars_after_longest_horizon():
    df = _frame()
    first = build_decision_outcome_label(_decision(), df)

    changed = df.copy()
    changed.iloc[9, changed.columns.get_loc("close")] = 900.0
    changed.iloc[9, changed.columns.get_loc("high")] = 901.0
    canonical = changed.reset_index().to_csv(
        index=False, float_format="%.17g", lineterminator="\n"
    )
    changed.attrs["data_provenance"] = {
        **df.attrs["data_provenance"],
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    second = build_decision_outcome_label(_decision(), changed)

    assert first["outcome_window_sha256"] == second["outcome_window_sha256"]
    assert first["label_id"] == second["label_id"]
    assert first["outcomes"] == second["outcomes"]


def test_label_rejects_missing_exact_decision_candle_instead_of_nearest_match():
    df = _frame().drop(pd.Timestamp("2026-01-01T02:00:00Z"))
    canonical = df.reset_index().to_csv(index=False, float_format="%.17g", lineterminator="\n")
    df.attrs["data_provenance"] = {
        **_frame().attrs["data_provenance"],
        "row_count": len(df),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    with pytest.raises(OutcomeLabelError, match="exact decision candle") as exc:
        build_decision_outcome_label(_decision(), df)
    assert exc.value.code == "DECISION_CANDLE_NOT_FOUND"


def test_label_rejects_incomplete_future_horizon():
    df = _frame(as_of=pd.Timestamp("2026-01-01T08:30:00Z"))
    with pytest.raises(OutcomeLabelError, match="longest outcome horizon"):
        build_decision_outcome_label(_decision(), df)


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("market_timestamp_source", "unavailable", "MISSING_MARKET_TIMESTAMP"),
        ("asset", "commodity", "UNSUPPORTED_ASSET"),
        ("timeframe", "", "MISSING_MARKET_METADATA"),
        ("exchange", "", "MISSING_EXCHANGE"),
    ],
)
def test_label_refuses_missing_or_unverified_market_metadata(field, value, expected_code):
    record = _decision()
    record[field] = value
    with pytest.raises(OutcomeLabelError) as exc:
        build_decision_outcome_label(record, _frame())
    assert exc.value.code == expected_code


def test_forex_label_requires_an_explicit_supported_data_source():
    record = _decision()
    record.update(
        asset="forex",
        symbol="EUR_USD",
        exchange="deriv",
    )
    with pytest.raises(OutcomeLabelError) as exc:
        build_decision_outcome_label(record, _frame())
    assert exc.value.code == "UNSUPPORTED_EXCHANGE"


def test_label_requires_provenance_to_match_the_recorded_market():
    df = _frame()
    df.attrs["data_provenance"]["exchange"] = "binance"
    with pytest.raises(OutcomeLabelError) as exc:
        build_decision_outcome_label(_decision(), df)
    assert exc.value.code == "DATA_PROVENANCE_MISMATCH"


def test_label_with_no_candidate_side_still_reports_market_outcomes():
    record = _decision()
    record["decision_context"] = {"analysis": {}, "trade_plan": {}, "strategy_router": {}}
    label = build_decision_outcome_label(record, _frame(), horizons_bars=(1, 3))
    assert label["candidate_side"] is None
    assert label["outcomes"]["1"]["candidate_side_net_return_bps"] is None
    assert label["outcomes"]["1"]["long_net_return_bps"] is not None
    assert label["outcomes"]["1"]["short_net_return_bps"] is not None


def test_yfinance_fx_label_rejects_ambiguous_underscore_symbol():
    record = _decision()
    record.update(asset="forex", symbol="EUR_USD", exchange="yfinance")
    with pytest.raises(OutcomeLabelError) as exc:
        build_decision_outcome_label(record, _frame())
    assert exc.value.code == "UNSUPPORTED_SYMBOL"


def test_label_rejects_data_whose_bytes_do_not_match_recorded_source_hash():
    df = _frame()
    df.iloc[3, df.columns.get_loc("close")] = 999.0
    with pytest.raises(OutcomeLabelError) as exc:
        build_decision_outcome_label(_decision(), df)
    assert exc.value.code == "DATA_PROVENANCE_MISMATCH"
