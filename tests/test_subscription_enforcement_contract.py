from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app/main.py"
DB = ROOT / "app/db.py"
MIGRATION = ROOT / "alembic/versions/0046_subscription_ai_usage.py"


def test_subscription_allowances_are_server_enforced():
    main = MAIN.read_text(encoding="utf-8")
    assert "async def _consume_ai_credit" in main
    assert "with_for_update()" in main
    assert "async def _enforce_exchange_limit" in main
    assert "async def _enforce_strategy_limit" in main
    assert '/api/customer/subscription/entitlements' in main
    assert 'await _require_feature(db, profile.id, "portfolio_analytics")' in main
    assert 'await _require_feature(db, profile.id, "portfolio_risk")' in main


def test_subscription_ai_meter_schema_and_migration_match():
    db = DB.read_text(encoding="utf-8")
    migration = MIGRATION.read_text(encoding="utf-8")
    assert "ai_credits_used" in db
    assert "ai_usage_period_start" in db
    assert 'revision = "0046_subscription_ai_usage"' in migration
    assert 'down_revision = "0034_withdrawal_provider_identity"' in migration
    assert 'op.add_column("subscriptions"' in migration


def test_exchange_connection_routes_enforce_limits_before_new_connection():
    main = MAIN.read_text(encoding="utf-8")
    for route, model in (("customer_connect_oanda", "CustomerOandaAccount"), ("customer_connect_deriv", "CustomerDerivAccount")):
        block = main[main.index("async def " + route):]
        assert model in block
        assert "if existing is None:" in block
        assert "await _enforce_exchange_limit(db, profile.id)" in block


def test_strategy_creation_paths_enforce_active_strategy_limit():
    main = MAIN.read_text(encoding="utf-8")
    for route in ("customer_start_bot", "create_customer_executor"):
        block = main[main.index("async def " + route):]
        assert "await _enforce_strategy_limit(db, profile.id)" in block


def test_ai_credit_is_consumed_only_after_successful_strategy_generation():
    main = MAIN.read_text(encoding="utf-8")
    block = main[main.index("async def create_strategy_candidate") :]
    assert block.index('except AIProviderError as exc:') < block.index('await _consume_ai_credit(db, profile.id, "strategy_builder_generation")')
