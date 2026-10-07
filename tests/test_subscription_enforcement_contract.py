from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_subscription_allowance_enforcement_is_present():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    db = (ROOT / "app/db.py").read_text(encoding="utf-8")
    migration = (ROOT / "alembic/versions/0046_subscription_ai_usage.py").read_text(encoding="utf-8")
    assert "async def _consume_ai_credit" in main
    assert "with_for_update()" in main
    assert "async def _enforce_exchange_limit" in main
    assert "async def _enforce_strategy_limit" in main
    assert "/api/customer/subscription/entitlements" in main
    assert "portfolio_analytics" in main
    assert "portfolio_risk" in main
    assert "ai_credits_used" in db
    assert "ai_usage_period_start" in db
    assert 'revision = "0046_subscription_ai_usage"' in migration
    assert 'down_revision = "0045_merge_application_heads"' in migration


def test_plan_capacity_contract_remains_explicit():
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert '"free": {"name": "Atlas Free", "monthly": 0.0, "annual": 0.0, "ai_credits": 100, "exchanges": 0, "strategies": 0' in main
    assert '"starter": {"name": "Atlas Starter", "monthly": 7.99, "annual": 79.90, "ai_credits": 1000, "exchanges": 1, "strategies": 1' in main
    assert '"pro": {"name": "Atlas Pro", "monthly": 17.99, "annual": 179.90, "ai_credits": 5000, "exchanges": 3, "strategies": 5' in main
    assert '"elite": {"name": "Atlas Elite", "monthly": 39.99, "annual": 399.90, "ai_credits": 20000, "exchanges": 10, "strategies": 20' in main
