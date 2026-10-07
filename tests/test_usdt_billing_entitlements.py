from pathlib import Path

from app.main import PLAN_DEFINITIONS


ROOT = Path(__file__).resolve().parents[1]


def test_verified_plan_catalog_has_explicit_trade_entitlement():
    assert PLAN_DEFINITIONS["free"]["monthly"] == 0.0
    assert PLAN_DEFINITIONS["free"]["monthly_trade_limit"] == 1
    assert PLAN_DEFINITIONS["starter"]["monthly_trade_limit"] == 0
    assert PLAN_DEFINITIONS["pro"]["monthly_trade_limit"] == 0
    assert PLAN_DEFINITIONS["elite"]["monthly_trade_limit"] == 0


def test_usdt_tron_is_the_only_subscription_payment_path():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert '/api/customer/billing/usdt-intent' in main
    assert '/api/customer/billing/usdt-submit' in main
    assert 'Stripe checkout is disabled. Atlas subscriptions accept USDT on TRON/TRC-20 only.' in main
    assert 'Stripe billing is disabled. Atlas subscriptions accept USDT on TRON/TRC-20 only.' in main
    assert '"payment_channels": ["USDT_TRON_TRC20"]' in main


def test_trade_entitlement_is_consumed_inside_trade_creation():
    execution = (ROOT / "app" / "execution.py").read_text(encoding="utf-8")
    assert "consume_subscription_trade" in execution
    assert 'idempotency_key=f"trade:{cid}"' in execution
    assert 'select(Trade).where(Trade.client_order_id == cid).with_for_update()' in execution


def test_billing_migration_is_single_child_of_current_application_head():
    migration = (ROOT / "alembic" / "versions" / "0034_usdt_subscription_entitlements.py").read_text(encoding="utf-8")
    assert 'revision = "0034_usdt_subscription_entitlements"' in migration
    assert 'down_revision = "0033_sweep_reconciliation_integrity"' in migration
    assert 'monthly_trade_limit' in migration
    assert '"payment_intents"' in migration
    assert '"usdt_payment_verifications"' in migration
