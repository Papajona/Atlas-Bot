from __future__ import annotations

from app.strategy_router import router_costs_bps
from app.trade_learning import learning_costs_bps
from app.trading_core import PROFILES


def test_commodity_learning_costs_follow_configured_profile() -> None:
    profile = PROFILES["commodity"]
    expected = float(profile.taker_bps) + float(profile.slippage_bps)
    assert learning_costs_bps("commodity") == expected


def test_commodity_router_costs_follow_configured_profile() -> None:
    profile = PROFILES["commodity"]
    expected = float(profile.taker_bps) + float(profile.slippage_bps)
    assert router_costs_bps("commodity") == expected


def test_router_unknown_asset_uses_supplied_fallback() -> None:
    assert router_costs_bps("not-a-configured-asset", crypto_bps=9.25) == 9.25


def test_learning_unknown_asset_falls_back_to_crypto_profile() -> None:
    profile = PROFILES["crypto"]
    expected = float(profile.taker_bps) + float(profile.slippage_bps)
    assert learning_costs_bps("not-a-configured-asset") == expected
