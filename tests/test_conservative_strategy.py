import numpy as np
import pandas as pd
import pytest

from app.conservative_strategy import (
    CONSERVATIVE_PROFILE,
    ConservativePolicyError,
    ConservativeRiskProfile,
    conservative_quantity,
    validate_conservative_order,
    execution_aware_backtest,
)


META = {
    "contract_multiplier": 1.0,
    "quote_to_account_rate": 1.0,
    "amount_step": 0.001,
    "min_amount": 0.001,
    "max_amount": 1000.0,
    "price_tick": 0.01,
}


def test_profile_is_immutable_and_conservative():
    with pytest.raises((AttributeError, TypeError)):
        CONSERVATIVE_PROFILE.risk_fraction = 0.02
    assert CONSERVATIVE_PROFILE.risk_fraction == 0.0025
    assert CONSERVATIVE_PROFILE.max_leverage == 1.0
    assert CONSERVATIVE_PROFILE.min_reward_risk == 2.0


def test_instrument_aware_sizing_obeys_risk_budget_and_step():
    quantity = conservative_quantity(
        equity=10_000, entry_price=100, stop_loss_price=98,
        take_profit_price=104, side="buy", metadata=META,
    )
    assert quantity == pytest.approx(12.5)
    assert quantity * 2 <= 10_000 * CONSERVATIVE_PROFILE.risk_fraction + 1e-8
    assert round(quantity / META["amount_step"]) == pytest.approx(quantity / META["amount_step"])


@pytest.mark.parametrize("metadata", [None, {}, {"contract_multiplier": 1}])
def test_unknown_or_incomplete_instrument_metadata_fails_closed(metadata):
    with pytest.raises(ConservativePolicyError):
        conservative_quantity(
            equity=10_000, entry_price=100, stop_loss_price=98,
            take_profit_price=104, side="buy", metadata=metadata,
        )


def test_invalid_rr_and_leverage_fail_closed():
    with pytest.raises(ConservativePolicyError, match="minimum_reward_risk"):
        conservative_quantity(
            equity=10_000, entry_price=100, stop_loss_price=98,
            take_profit_price=102, side="buy", metadata=META,
        )
    huge_meta = {**META, "contract_multiplier": 100.0, "max_amount": 1_000_000}
    with pytest.raises(ConservativePolicyError, match="maximum_leverage"):
        conservative_quantity(
            equity=10_000, entry_price=100, stop_loss_price=99.99,
            take_profit_price=100.02, side="buy", metadata=huge_meta,
        )


def test_central_order_policy_rejects_quantity_above_safe_size_and_missing_spread():
    kwargs = dict(
        equity=10_000, entry_price=100, stop_loss_price=98,
        take_profit_price=104, side="buy", metadata=META,
    )
    with pytest.raises(ConservativePolicyError, match="quantity_exceeds_conservative_size"):
        validate_conservative_order(**kwargs, quantity=20, spread_bps=5)
    with pytest.raises(ConservativePolicyError, match="spread_missing"):
        validate_conservative_order(**kwargs, quantity=12.5, spread_bps=None)


def test_execution_aware_backtest_next_open_and_stop_wins_if_both_hit():
    bars = pd.DataFrame({
        "open": [100, 99, 100],
        "high": [101, 105, 102],
        "low": [99, 95, 99],
        "close": [100, 101, 101],
        "signal": [1, 0, 0],
        "stop_loss": [98, np.nan, np.nan],
        "take_profit": [104, np.nan, np.nan],
    })
    result = execution_aware_backtest(bars, fee_bps=0, slippage_bps=0)
    assert result.iloc[0]["entry_price"] == pytest.approx(99)
    assert result.iloc[0]["exit_price"] == pytest.approx(98)
    assert result.iloc[0]["trade_return"] == pytest.approx(98 / 99 - 1)
