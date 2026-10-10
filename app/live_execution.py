from __future__ import annotations

from sqlalchemy import select
from datetime import datetime, timezone

from .config import settings
from .db import AppState, TradingAccount, CustomerProfile, CustomerBinanceAccount, Subscription, Plan


class LiveExecutionBlocked(RuntimeError):
    pass


async def assert_live_system_enabled(
    db,
    *,
    asset: str,
    customer_id: int | None,
    exchange: str,
    side: str,
    quantity: float,
    reduce_only: bool = False,
    practice_demo: bool = False,
) -> None:
    """Final deny-by-default gate for live orders and explicitly enabled OANDA practice orders."""
    state = (await db.execute(select(AppState).where(AppState.id == 1).with_for_update())).scalar_one_or_none()
    if not state:
        raise LiveExecutionBlocked("Live execution state is unavailable")

    normalized_asset = str(asset or "crypto").lower()
    normalized_exchange = str(exchange or "").lower()
    if normalized_asset in {"forex", "commodity"}:
        # Practice orders are deliberately separate from the real-money live-trading gate:
        # they must use the practice endpoint and platform account only, and must never be
        # authorized for a customer or by the live-trading flags.
        if not practice_demo:
            raise LiveExecutionBlocked("OANDA Forex/commodity execution is demo-only in AtlasRisk")
        if normalized_exchange != "oanda":
            raise LiveExecutionBlocked("Forex/commodity practice execution requires the OANDA venue")
        if customer_id is not None:
            raise LiveExecutionBlocked("Customer OANDA execution requires a separately verified customer practice-account flow")
        if state.kill_switch:
            raise LiveExecutionBlocked("OANDA practice execution is halted by the platform kill switch")
        if not bool(state.forex_demo_enabled):
            raise LiveExecutionBlocked("OANDA practice execution is not enabled in platform state")
        if not settings.oanda_practice or settings.forex_live_enabled:
            raise LiveExecutionBlocked("OANDA practice mode must be enabled and live OANDA must remain disabled")
        if not settings.oanda_account_id or not settings.oanda_api_token:
            raise LiveExecutionBlocked("OANDA practice credentials are not configured")
        if quantity <= 0 or side not in {"buy", "sell"}:
            raise LiveExecutionBlocked("Invalid OANDA practice order parameters")
        return

    if state.kill_switch or not state.live_enabled:
        raise LiveExecutionBlocked("Live trading is halted by the platform risk state")
    if not settings.live_trading_enabled or settings.paper_trading or settings.broker_sandbox:
        raise LiveExecutionBlocked("Live execution is disabled by platform configuration")
    if quantity <= 0 or side not in {"buy", "sell"}:
        raise LiveExecutionBlocked("Invalid live order parameters")

    if customer_id is None:
        if not settings.exchange_api_key or not settings.exchange_api_secret:
            raise LiveExecutionBlocked("Platform exchange credentials are not configured")
        return

    if not settings.customer_live_trading_enabled:
        raise LiveExecutionBlocked("Customer live trading is not enabled")

    profile = (await db.execute(select(CustomerProfile).where(CustomerProfile.id == customer_id).with_for_update())).scalar_one_or_none()
    if not profile or str(profile.status).upper() != "ACTIVE":
        raise LiveExecutionBlocked("Customer account is not active")

    account = (await db.execute(select(TradingAccount).where(TradingAccount.customer_id == customer_id).with_for_update())).scalar_one_or_none()
    if not account:
        raise LiveExecutionBlocked("Customer trading account is unavailable")
    account_status = str(account.status).upper()
    if account_status != "ACTIVE" and not (account_status == "HALTED" and reduce_only):
        raise LiveExecutionBlocked("Customer trading account is not active")

    pilot_status = str(account.pilot_status or "NONE").upper()
    pilot_expires_at = account.pilot_expires_at
    if pilot_status != "APPROVED":
        raise LiveExecutionBlocked("Customer is not approved for the live pilot")
    if not account.pilot_requested_by or not account.pilot_approved_by or account.pilot_requested_by == account.pilot_approved_by:
        raise LiveExecutionBlocked("Customer pilot approval is not valid")
    if pilot_expires_at is None:
        raise LiveExecutionBlocked("Customer pilot expiry is not configured")
    if pilot_expires_at.tzinfo is None:
        pilot_expires_at = pilot_expires_at.replace(tzinfo=timezone.utc)
    if pilot_expires_at <= datetime.now(timezone.utc):
        raise LiveExecutionBlocked("Customer live pilot has expired")

    sub = (await db.execute(
        select(Subscription).where(
            Subscription.customer_id == customer_id,
            Subscription.status.in_(["trialing", "active", "past_due"]),
        ).order_by(Subscription.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not sub:
        raise LiveExecutionBlocked("Customer live-trading subscription is not active")
    if str(sub.status).lower() == "trialing" and getattr(sub, "trial_end", None) is not None:
        trial_end = sub.trial_end
        if trial_end.tzinfo is None:
            trial_end = trial_end.replace(tzinfo=timezone.utc)
        if trial_end <= datetime.now(timezone.utc):
            raise LiveExecutionBlocked("Customer trial entitlement has expired")
    plan = (await db.execute(select(Plan).where(Plan.code == sub.plan_code))).scalar_one_or_none()
    if not plan or not bool(plan.active) or not bool(plan.live_trading):
        raise LiveExecutionBlocked("Customer plan does not permit live trading")

    if normalized_asset == "crypto":
        if normalized_exchange != "binance":
            raise LiveExecutionBlocked("Customer live crypto trading requires the verified Binance isolation path")
        customer_binance = (await db.execute(
            select(CustomerBinanceAccount).where(CustomerBinanceAccount.customer_id == customer_id).with_for_update()
        )).scalar_one_or_none()
        if not customer_binance:
            raise LiveExecutionBlocked("Verified customer Binance account is missing")
        if str(customer_binance.status).upper() != "VERIFIED" or not bool(customer_binance.can_trade):
            raise LiveExecutionBlocked("Customer Binance account is not verified for trading")
        if bool(customer_binance.enable_withdrawals) or bool(customer_binance.universal_transfer):
            raise LiveExecutionBlocked("Customer Binance key has forbidden transfer/withdrawal permissions")
