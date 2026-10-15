"""Regression tests for the 15 Oct research-bias audit fixes."""
import pandas as pd

from app.strategy_engine import StrategyConfig, risk_scaled_position, target_positions
from app.research_engine import _apply_protective_halts
from app.live_fees import ESTIMATED, extract_order_fee_quote


def test_live_sizing_matches_capped_backtest_sizing_at_low_volatility():
    cfg = StrategyConfig(target_vol_annual=0.15, max_leverage=1.5, signal_threshold=0.05)
    assert risk_scaled_position(0.5, 0.05, cfg) == 0.75


def test_signal_threshold_variants_change_position_series():
    raw = pd.Series([0.04, 0.06, 0.09], index=pd.RangeIndex(3))
    vol = pd.Series([0.05, 0.05, 0.05], index=raw.index)
    low = StrategyConfig(target_vol_annual=0.15, max_leverage=1.5, signal_threshold=0.04)
    high = StrategyConfig(target_vol_annual=0.15, max_leverage=1.5, signal_threshold=0.08)
    assert not target_positions(raw, vol, low).equals(target_positions(raw, vol, high))


def test_daily_loss_halt_resets_on_next_utc_date_but_drawdown_halt_is_separate():
    index = pd.DatetimeIndex([
        "2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z",
        "2026-01-01T12:00:00Z", "2026-01-02T10:00:00Z",
    ])
    position = pd.Series(1.0, index=index)
    returns = pd.Series([-0.04, 0.01, 0.01, 0.01], index=index)
    protected, _, diagnostic = _apply_protective_halts(
        position, returns, cost_rate=0.0, drawdown_limit=0.50,
        daily_loss_limit=0.03, loss_event_threshold=99, cooldown_bars=0,
    )
    assert protected.tolist() == [1.0, 0.0, 0.0, 1.0]
    assert diagnostic["hard_halt"] is False


def test_unparseable_exchange_fee_is_estimated_not_exact_zero():
    fee, quality, note = extract_order_fee_quote(
        {"fee": {"cost": "n/a", "currency": "USDT"}},
        "BTC/USDT", 60000.0, 0.1, fallback_taker_bps=10.0,
    )
    assert quality == ESTIMATED
    assert fee == 6.0
    assert "unparsable" in note



def test_regime_router_uses_shared_risk_sized_positions(monkeypatch):
    import numpy as np
    from app import strategy_router
    from app.strategy_engine import next_open_returns

    cfg = StrategyConfig(
        target_vol_annual=0.15,
        max_leverage=1.5,
        signal_threshold=0.25,
    )
    df = pd.DataFrame(
        {"open": [100.0, 102.0, 101.0, 105.0]},
        index=pd.RangeIndex(4),
    )
    signals = pd.DataFrame(
        {
            "trend": [0.10, 0.80, 0.20, 0.90],
            "realized_vol": [0.05, 0.05, 0.05, 0.05],
        },
        index=df.index,
    )
    monkeypatch.setattr(
        strategy_router,
        "strategy_signals",
        lambda frame, config, asset: signals,
    )

    actual = strategy_router._strategy_returns(
        df, cfg, "trend", costs_bps=0.0, asset="crypto"
    )
    expected_positions = target_positions(
        signals["trend"], signals["realized_vol"], cfg
    ).shift(1).fillna(0.0)
    expected = expected_positions * (next_open_returns(df) - 1.0)

    np.testing.assert_allclose(actual.to_numpy(), expected.to_numpy())
