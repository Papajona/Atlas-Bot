from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.live_execution import LiveExecutionBlocked, require_live_subscription_entitlement


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def subscription(**overrides):
    values = {
        "status": "active",
        "current_period_end": NOW + timedelta(days=30),
        "trial_end": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_active_subscription_with_future_period_is_entitled():
    require_live_subscription_entitlement(subscription(), now=NOW)


def test_trialing_subscription_requires_both_future_period_and_trial_end():
    trial_end = NOW + timedelta(days=3)
    require_live_subscription_entitlement(
        subscription(status="trialing", trial_end=trial_end), now=NOW
    )


@pytest.mark.parametrize("status", ["past_due", "canceled", "cancelled", "unpaid", "incomplete", "paused", ""])
def test_non_active_subscription_statuses_are_blocked(status):
    with pytest.raises(LiveExecutionBlocked, match="subscription is not active"):
        require_live_subscription_entitlement(subscription(status=status), now=NOW)


def test_expired_subscription_period_is_blocked():
    with pytest.raises(LiveExecutionBlocked, match="period has expired"):
        require_live_subscription_entitlement(
            subscription(current_period_end=NOW), now=NOW
        )


def test_missing_subscription_period_end_is_blocked():
    with pytest.raises(LiveExecutionBlocked, match="period end is not configured"):
        require_live_subscription_entitlement(
            subscription(current_period_end=None), now=NOW
        )


def test_trial_without_expiry_is_blocked():
    with pytest.raises(LiveExecutionBlocked, match="trial expiry is not configured"):
        require_live_subscription_entitlement(
            subscription(status="trialing", trial_end=None), now=NOW
        )


def test_expired_trial_is_blocked_even_if_period_end_is_future():
    with pytest.raises(LiveExecutionBlocked, match="trial entitlement has expired"):
        require_live_subscription_entitlement(
            subscription(status="trialing", trial_end=NOW), now=NOW
        )


def test_naive_database_timestamps_are_treated_as_utc():
    require_live_subscription_entitlement(
        subscription(current_period_end=(NOW + timedelta(days=1)).replace(tzinfo=None)),
        now=NOW,
    )


def test_binance_customer_subaccount_provisioning_defaults_off():
    settings = Settings(environment="test")
    assert settings.binance_customer_subaccounts_enabled is False
    assert settings.customer_live_trading_enabled is False


def test_customer_live_execution_fails_startup_until_platform_account_model_exists():
    with pytest.raises(ValidationError, match="approved platform-owned account allocation model"):
        Settings(
            environment="test",
            customer_live_trading_enabled=True,
            live_trading_enabled=True,
            paper_trading=False,
            broker_sandbox=False,
        )
