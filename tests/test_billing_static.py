from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[1]

def test_billing_source_parses():
    for rel in ['app/main.py','app/db.py','app/config.py','alembic/versions/0013_billing_referrals_margin.py']:
        ast.parse((ROOT/rel).read_text())

def test_internal_plan_and_finance_routes_present():
    s=(ROOT/'app/main.py').read_text()
    for route in ['/api/plans','/api/customer/billing','/api/customer/referral/code','/api/customer/referral/claim','/api/customer/referrals','/api/admin/billing/cost','/api/admin/billing/revenue','/api/admin/billing/margin']:
        assert route in s

def test_margin_formula_present():
    s=(ROOT/'app/main.py').read_text()
    assert 'contribution=revenue-costs-commissions' in s
    assert 'contribution/revenue*100' in s

def test_recurring_referrals_are_invoice_scoped():
    s=(ROOT/'app/db.py').read_text()
    assert 'uq_referral_commission_invoice' in s
    assert 'provider_reference' in s


def test_stripe_payment_routes_and_config_are_removed():
    main=(ROOT/'app/main.py').read_text()
    config=(ROOT/'app/config.py').read_text()
    customer=(ROOT/'app/templates/customer.html').read_text()
    strings=(ROOT/'app/src/main/res/values/strings.xml').read_text()
    assert '/api/customer/billing/checkout' not in main
    assert '/api/billing/stripe/webhook' not in main
    assert 'stripe_' not in config
    assert 'checkoutPlan' not in customer
    assert 'checkout.stripe.com' not in strings
