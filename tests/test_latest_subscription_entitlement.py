from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.main import _get_active_subscription


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeDB:
    def __init__(self, subscription):
        self.subscription = subscription
        self.statement = None
        self.flushed = False

    async def execute(self, statement):
        self.statement = statement
        return _Result(self.subscription)

    async def flush(self):
        self.flushed = True


def _subscription(status="active", period_end=None, trial_end=None, plan_code="pro"):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=2,
        customer_id=7,
        status=status,
        plan_code=plan_code,
        current_period_end=period_end if period_end is not None else now + timedelta(days=30),
        trial_end=trial_end,
        current_period_start=now - timedelta(days=1),
    )


@pytest.mark.asyncio
async def test_newest_cancelled_subscription_is_not_entitled_and_query_does_not_filter_status():
    cancelled = _subscription(status="cancelled")
    db = _FakeDB(cancelled)

    assert await _get_active_subscription(db, 7) is None

    sql = str(db.statement)
    assert "subscriptions.customer_id" in sql
    assert "subscriptions.status IN" not in sql
    assert "subscriptions.created_at DESC" in sql
    assert "subscriptions.id DESC" in sql


@pytest.mark.asyncio
async def test_expired_active_subscription_does_not_grant_paid_entitlements():
    expired = _subscription(period_end=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await _get_active_subscription(_FakeDB(expired), 7) is None


@pytest.mark.asyncio
async def test_current_active_subscription_remains_eligible():
    active = _subscription()
    assert await _get_active_subscription(_FakeDB(active), 7) is active


@pytest.mark.asyncio
async def test_expired_trial_is_downgraded_to_free_and_returns_no_paid_entitlement():
    expired_trial = _subscription(
        status="trialing",
        period_end=datetime.now(timezone.utc) + timedelta(days=1),
        trial_end=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db = _FakeDB(expired_trial)

    assert await _get_active_subscription(db, 7) is None
    assert expired_trial.status == "active"
    assert expired_trial.plan_code == "free"
    assert expired_trial.current_period_end <= datetime.now(timezone.utc)
    assert db.flushed


@pytest.mark.asyncio
async def test_trial_without_expiry_is_fail_closed():
    trial = _subscription(status="trialing", trial_end=None)
    assert await _get_active_subscription(_FakeDB(trial), 7) is None
