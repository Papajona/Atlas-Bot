import numpy as np
import pandas as pd
import pytest

from app.execution_aware_research import (
    costed_execution_returns,
    execution_turnover,
    simulate_execution_aware_strategy,
)


def _ohlc(opens, highs, lows):
    idx = pd.date_range("2026-01-01T00:00:00Z", periods=len(opens), freq="h")
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": opens,
        "volume": np.full(len(opens), 1000.0),
    }, index=idx)


def test_ambiguous_bar_uses_stop_before_target_and_charges_exit_once():
    frame = _ohlc(
        opens=[100.0, 100.0, 100.0],
        highs=[102.0, 108.0, 102.0],
        lows=[99.0, 94.0, 99.0],
    )
    positions = pd.Series([1.0, 1.0, 1.0], index=frame.index)
    atr = pd.Series([2.0, 2.0, 2.0], index=frame.index)

    result = simulate_execution_aware_strategy(
        frame, positions, atr,
        stop_atr=2.0, take_profit_atr=3.0,
        transaction_cost_bps=10.0, carry_bps_per_bar=0.0,
    )
    assert result["audit"]["stop_loss_exits"] == 1
    assert result["audit"]["take_profit_exits"] == 0
    assert result["audit"]["ambiguous_bars_filled_stop_first"] == 1
    # Conservative OHLC stop-first handling does not prove tick-level event ordering.
    assert result["audit"]["intrabar_stop_target_model_complete"] is False
    assert result["market_returns"].iloc[1] == pytest.approx(-0.04)
    # Position entry is charged on bar zero and the intrabar close on bar one.
    assert result["turnover"].iloc[0] == pytest.approx(1.0)
    assert result["turnover"].iloc[1] == pytest.approx(1.0)
    assert result["positions"].iloc[2] == 0.0
    assert result["audit"]["financing_cost_model_complete"] is True


def test_short_target_gap_uses_next_observed_open_not_target_price():
    frame = _ohlc(
        opens=[100.0, 100.0, 93.0],
        highs=[101.0, 103.0, 95.0],
        lows=[99.0, 95.0, 92.0],
    )
    positions = pd.Series([-1.0, -1.0, -1.0], index=frame.index)
    atr = pd.Series([2.0, 2.0, 2.0], index=frame.index)

    result = simulate_execution_aware_strategy(
        frame, positions, atr,
        stop_atr=2.0, take_profit_atr=3.0,
        transaction_cost_bps=0.0, carry_bps_per_bar=0.0,
    )
    assert result["audit"]["take_profit_exits"] == 1
    assert result["audit"]["gap_exits"] == 1
    assert result["market_returns"].iloc[1] == pytest.approx(93.0 / 100.0 - 1.0)
    assert result["net_returns"].iloc[1] == pytest.approx(0.07)


def test_missing_carry_is_not_marked_financing_complete():
    frame = _ohlc(
        opens=[100.0, 101.0],
        highs=[101.0, 102.0],
        lows=[99.0, 100.0],
    )
    positions = pd.Series([1.0, 1.0], index=frame.index)
    atr = pd.Series([2.0, 2.0], index=frame.index)
    result = simulate_execution_aware_strategy(
        frame, positions, atr,
        stop_atr=2.0, take_profit_atr=3.0,
        transaction_cost_bps=0.0, carry_bps_per_bar=None,
    )
    assert result["audit"]["financing_cost_model_complete"] is False
    assert result["audit"]["promotion_eligible_on_cost_model"] is False


def test_cost_stress_includes_carry_and_exit_turnover():
    index = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    positions = pd.Series([1.0, 1.0, 0.0], index=index)
    exits = pd.Series([False, True, False], index=index)
    market = pd.Series([0.0, 0.0, 0.0], index=index)

    turnover = execution_turnover(positions, exits)
    assert turnover.tolist() == pytest.approx([1.0, 1.0, 0.0])
    net = costed_execution_returns(
        positions, market,
        transaction_cost_bps=10.0,
        carry_bps_per_bar=2.0,
        exit_mask=exits,
        cost_multiplier=2.0,
    )
    assert net.tolist() == pytest.approx([-0.0024, -0.0024, 0.0])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_invalid_transaction_costs_fail_closed(bad):
    index = pd.date_range("2026-01-01", periods=1, freq="h", tz="UTC")
    with pytest.raises(ValueError):
        costed_execution_returns(
            pd.Series([1.0], index=index),
            pd.Series([0.0], index=index),
            transaction_cost_bps=bad,
            carry_bps_per_bar=0.0,
        )



def test_oos_financing_completeness_requires_traceable_source_and_evidence_id():
    from app.research_validation import fixed_signal_walk_forward_oos

    n = 360
    index = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    opens = 100.0 * np.power(1.001, np.arange(n, dtype=float))
    frame = pd.DataFrame({
        "open": opens,
        "high": opens * 1.002,
        "low": opens * 0.998,
        "close": opens,
        "volume": np.full(n, 1000.0),
    }, index=index)
    positions = pd.Series(0.5, index=index)
    returns = pd.Series(0.002, index=index)
    exits = pd.Series(False, index=index)

    incomplete = fixed_signal_walk_forward_oos(
        frame, positions, costs_bps=2.0, folds=3, min_train=100,
        annualization_factor=8760.0, taker_bps=1.0, slippage_bps=1.0,
        positions_are_final=True, bar_returns=returns, intrabar_exit=exits,
        carry_bps_per_bar=2.0,
    )
    assert incomplete["status"] == "OK"
    assert incomplete["carry_rate_configured"] is True
    assert incomplete["financing_cost_model_complete"] is False
    assert incomplete["execution_cost_model_complete"] is False
    assert incomplete["cost_stress_ok"] is False

    complete = fixed_signal_walk_forward_oos(
        frame, positions, costs_bps=2.0, folds=3, min_train=100,
        annualization_factor=8760.0, taker_bps=1.0, slippage_bps=1.0,
        positions_are_final=True, bar_returns=returns, intrabar_exit=exits,
        carry_bps_per_bar=2.0,
        carry_evidence_source="fixture:venue-financing-schedule",
        carry_evidence_id="fixture-rate-sheet-v1",
    )
    assert complete["financing_cost_model_complete"] is True
    assert complete["execution_cost_model_complete"] is False
    assert complete["execution_cost_calibration_status"] == "MISSING_VENUE_CALIBRATION_EVIDENCE"
    assert complete["execution_cost_calibration_evidence_id"] is None
    assert complete["carry_evidence_source"] == "fixture:venue-financing-schedule"
    assert complete["carry_evidence_id"] == "fixture-rate-sheet-v1"



def test_last_evaluated_bar_liquidates_at_observed_close_and_charges_exit():
    frame = _ohlc(
        opens=[100.0],
        highs=[104.0],
        lows=[99.0],
    )
    frame["close"] = [103.0]
    positions = pd.Series([1.0], index=frame.index)
    atr = pd.Series([2.0], index=frame.index)
    result = simulate_execution_aware_strategy(
        frame, positions, atr,
        stop_atr=2.0, take_profit_atr=3.0,
        transaction_cost_bps=10.0, carry_bps_per_bar=0.0,
    )
    assert result["market_returns"].iloc[0] == pytest.approx(0.03)
    assert result["turnover"].iloc[0] == pytest.approx(2.0)  # entry plus terminal liquidation
    assert result["net_returns"].iloc[0] == pytest.approx(0.028)
    assert result["audit"]["terminal_close_exits"] == 1
    assert result["audit"]["final_equity"] == pytest.approx(1.028)
