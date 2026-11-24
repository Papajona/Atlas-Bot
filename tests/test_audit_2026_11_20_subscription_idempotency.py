"""Regression for repeated initialization after an internal trial expires."""
import asyncio
from types import SimpleNamespace

from app import main


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ExistingSubscriptionDB:
    def __init__(self, existing):
        self.existing = existing
        self.selects = 0
        self.adds = 0
        self.savepoints = 0

    async def execute(self, _statement):
        self.selects += 1
        return _Result(self.existing)

    def add(self, _row):
        self.adds += 1

    async def flush(self):
        raise AssertionError("an existing stable free/trial subscription must not be inserted again")

    def begin_nested(self):
        self.savepoints += 1
        raise AssertionError("an existing stable free/trial subscription must not open an insert savepoint")


def test_repeated_expired_trial_initialization_reuses_the_stable_free_row(monkeypatch):
    async def no_plans(_db):
        return None

    async def no_active_subscription(_db, _customer_id):
        return None

    monkeypatch.setattr(main, "_ensure_billing_plans", no_plans)
    monkeypatch.setattr(main, "_get_active_subscription", no_active_subscription)
    monkeypatch.setattr(main.settings, "billing_trial_days", 14)

    existing = SimpleNamespace(
        customer_id=42,
        provider="internal",
        provider_subscription_id="atlas-free-42",
        plan_code="free",
        status="active",
    )
    db = _ExistingSubscriptionDB(existing)
    profile = SimpleNamespace(id=42)

    async def run():
        first = await main._ensure_customer_subscription(db, profile)
        second = await main._ensure_customer_subscription(db, profile)
        assert first is existing
        assert second is existing

    asyncio.run(run())
    assert db.selects == 2
    assert db.adds == 0
    assert db.savepoints == 0
