"""Regression tests for fail-closed TRON treasury address ownership."""
from decimal import Decimal
from types import SimpleNamespace
import asyncio

import pytest


def test_unknown_treasury_role_cannot_be_used_for_sweeps(monkeypatch):
    from app.config import settings
    from app.tron_sweep import SweepError, build_sweep_intent

    monkeypatch.setattr(settings, "usdt_tron_treasury_address_role", "unknown")
    with pytest.raises(SweepError, match="verified Atlas-controlled"):
        build_sweep_intent(
            wallet_id=7,
            source_address="TSource",
            amount_usdt=Decimal("10"),
        )


def test_binance_deposit_role_cannot_be_used_as_atlas_sweep_destination(monkeypatch):
    from app.config import settings
    from app.tron_sweep import SweepError, build_sweep_intent

    monkeypatch.setattr(settings, "usdt_tron_treasury_address_role", "binance_deposit")
    with pytest.raises(SweepError, match="verified Atlas-controlled"):
        build_sweep_intent(
            wallet_id=7,
            source_address="TSource",
            amount_usdt=Decimal("10"),
            treasury_address="TExplicitOverrideDoesNotBypassRole",
        )


def test_custody_addresses_exclude_binance_deposit_even_if_wallet_references_it():
    from app.custody_reconciliation import _custody_addresses

    treasury = "TExternalBinanceAddress"
    wallets = [
        SimpleNamespace(deposit_address=treasury),
        SimpleNamespace(deposit_address="TAtlasVirtualWallet"),
        SimpleNamespace(deposit_address="TAtlasVirtualWallet"),
    ]
    assert _custody_addresses(
        wallets=wallets,
        treasury_address=treasury,
        treasury_address_role="binance_deposit",
    ) == ["TAtlasVirtualWallet"]


def test_custody_addresses_include_treasury_only_when_atlas_controlled():
    from app.custody_reconciliation import _custody_addresses

    treasury = "TAtlasTreasury"
    wallets = [SimpleNamespace(deposit_address="TAtlasVirtualWallet")]
    assert _custody_addresses(
        wallets=wallets,
        treasury_address=treasury,
        treasury_address_role="atlas_controlled",
    ) == [treasury, "TAtlasVirtualWallet"]


def test_unknown_role_returns_unknown_without_provider_requests(monkeypatch):
    from app.config import settings
    from app.custody_reconciliation import reconcile_usdt_custody
    from app.db import init_db, SessionLocal

    monkeypatch.setattr(settings, "usdt_tron_treasury_address_role", "unknown")

    async def run():
        await init_db()
        async with SessionLocal() as db:
            report = await reconcile_usdt_custody(db)
            assert report["status"] == "UNKNOWN"
            assert report["addresses_checked"] == 0
            assert report["treasury_address_role"] == "unknown"
            assert "ownership is unverified" in report["error"]
            await db.rollback()

    asyncio.run(run())


def test_unknown_custody_role_is_rejected_by_address_builder():
    from app.custody_reconciliation import _custody_addresses

    with pytest.raises(ValueError, match="role is not verified"):
        _custody_addresses(
            wallets=[],
            treasury_address="TUnverified",
            treasury_address_role="unknown",
        )
