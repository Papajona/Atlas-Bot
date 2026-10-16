import numpy as np
import pandas as pd

from app.execution_aware_research import simulate_execution_aware_strategy


def _run(day1_gap):
    idx = pd.date_range("2025-01-01", periods=24 * 8, freq="1h", tz="UTC")
    o = np.full(len(idx), 100.0)
    c = o.copy()
    if day1_gap:
        o[5:] = 94.0
        c[5:] = 94.0  # gap through the stop on day 1 -> -6% -> daily-loss halt
    base = o[-1]
    k = 24 * 4 + 22  # day 5, 22:00: ordinary stop-out, below the 3% daily limit
    low = np.minimum(o, c) * 0.9995
    high = np.maximum(o, c) * 1.0005
    low[k] = base * 0.85
    c[k] = base * 0.85
    o[k + 1:] = base * 0.85
    c[k + 1:] = base * 0.85
    high = np.maximum.reduce([high, o, c])
    low = np.minimum.reduce([low, o, c])
    df = pd.DataFrame({"open": o, "high": high, "low": low, "close": c, "volume": 1.0}, index=idx)
    return simulate_execution_aware_strategy(
        df,
        pd.Series(1.0, index=idx),
        pd.Series(base * 0.01, index=idx),
        stop_atr=2.5,
        take_profit_atr=4.0,
        transaction_cost_bps=0.0,
        carry_bps_per_bar=0.0,
        drawdown_limit=0.5,
        daily_loss_limit=0.03,
    )


def test_reentry_block_is_not_cleared_on_later_days_after_an_earlier_daily_halt():
    control = _run(False)["positions"]
    halted = _run(True)
    assert halted["audit"]["risk_halt_reason"] == "DAILY_LOSS"
    # The ordinary stop-out on day 5 must behave consistently in both runs.
    assert (control.iloc[120:] == 0).all()
    assert (halted["positions"].iloc[120:] == 0).all()


def test_daily_halt_still_allows_reentry_on_the_next_utc_day():
    pos = _run(True)["positions"]
    assert (pos.iloc[24:48] != 0).any()
