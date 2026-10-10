from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
TRUTH = (ROOT / "docs" / "SUBSCRIPTION_PRODUCT_TRUTH.md").read_text(encoding="utf-8")
MATRIX = (ROOT / "docs" / "SUBSCRIPTION_VERIFICATION_MATRIX.md").read_text(encoding="utf-8")
COMPETITIVE = (ROOT / "docs" / "COMPETITIVE_POSITIONING_AND_MOAT.md").read_text(encoding="utf-8")


def test_subscription_plan_values_are_explicit():
    expected = {
        "free": (0.0, 100, 0),
        "starter": (7.99, 1000, 1),
        "pro": (17.99, 5000, 3),
        "elite": (39.99, 20000, 10),
    }
    for code, (monthly, credits, exchanges) in expected.items():
        start = MAIN.index(f'"{code}": {{')
        block = MAIN[start:MAIN.index('},', start) + 2]
        assert f'"monthly": {monthly}' in block
        assert f'"ai_credits": {credits}' in block
        assert f'"exchanges": {exchanges}' in block


def test_verified_entitlement_paths_are_present():
    assert "async def _consume_ai_credit" in MAIN
    assert "async def _enforce_exchange_limit" in MAIN
    assert "async def _enforce_strategy_limit" in MAIN
    assert "/api/customer/subscription/entitlements" in MAIN
    assert '_require_feature(db, profile.id, "portfolio_analytics")' in MAIN
    assert '_require_feature(db, profile.id, "portfolio_risk")' in MAIN
    assert 'await _enforce_exchange_limit(db, req.customer_id)' in MAIN
    assert 'await _enforce_exchange_limit(db, profile.id)' in MAIN
    assert 'await _enforce_strategy_limit(db, profile.id)' in MAIN


def test_operational_boundaries_are_documented():
    assert "staging E2E evidence" in TRUTH
    assert "Wallet funding and trading-customer ledger flows are separate from subscription monetization." in TRUTH
    assert "this wording does not mean the existing customer USDT/TRON deposit and ledger code should be removed" in TRUTH
    assert "The Deriv crypto-wallet payment path is not verified as implemented end to end" in TRUTH
    assert "Do not market these as exclusive" in TRUTH
    assert "Do not advertise" in MATRIX


def test_competitive_document_requires_official_source_and_date():
    # The current document deliberately avoids competitor price claims.
    assert "official sources" in COMPETITIVE
    assert "official source and date" in COMPETITIVE
