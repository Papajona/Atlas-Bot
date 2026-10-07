from decimal import Decimal

import pytest

from app import config
from app.tron_sweep import SweepError, build_sweep_intent


def test_binance_deposit_role_blocks_tron_sweep(monkeypatch):
    monkeypatch.setattr(config.settings, "usdt_tron_treasury_address_role", "binance_deposit")
    with pytest.raises(SweepError, match="not Atlas-controlled"):
        build_sweep_intent(
            wallet_id=7,
            source_address="TSource",
            amount_usdt=Decimal("10"),
        )


def test_atlas_controlled_role_allows_sweep_intent(monkeypatch):
    monkeypatch.setattr(config.settings, "usdt_tron_treasury_address_role", "atlas_controlled")
    intent = build_sweep_intent(
        wallet_id=7,
        source_address="TSource",
        amount_usdt=Decimal("10"),
        treasury_address="TTreasury",
    )
    assert intent.amount_usdt == Decimal("10")
    assert intent.treasury_address == "TTreasury"
