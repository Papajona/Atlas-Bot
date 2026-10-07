from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "main.py").read_text()
TRUTH = (ROOT / "docs" / "SUBSCRIPTION_PRODUCT_TRUTH.md").read_text()
MATRIX = (ROOT / "docs" / "SUBSCRIPTION_VERIFICATION_MATRIX.md").read_text()


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


def test_paid_feature_routes_have_server_entitlement_gates():
    protected = {
        "smart_trade": '@app.post("/api/customer/smart-trades")',
        "grid_bot": '@app.post("/api/customer/grid-bots")',
        "arbitrage": '@app.post("/api/customer/arbitrage/paper")',
        "alerts": '@app.post("/api/customer/alerts")',
        "market_scanner": '@app.get("/api/customer/market-scanner")',
        "strategy_builder": '@app.post("/api/customer/strategy-lab/candidates")',
        "webhooks": '@app.post("/api/customer/webhooks")',
        "dca_bot": '@app.post("/api/customer/dca-bots")',
    }
    for feature, route in protected.items():
        start = MAIN.index(route)
        # Route blocks are intentionally bounded by the next FastAPI decorator.
        end = MAIN.find("\n@app.", start + len(route))
        block = MAIN[start:] if end == -1 else MAIN[start:end]
        assert f'_require_feature(db, profile.id, "{feature}")' in block or \
               f'_require_feature(db,profile.id,"{feature}")' in block, feature


def test_known_operational_limitations_are_documented():
    assert "paper planning" in TRUTH
    assert "configuration" in TRUTH
    assert "Binance triangular-arbitrage live execution is disabled" in TRUTH
    assert "AI credit allowance enforcement" in TRUTH
    assert "exchange-count enforcement" in TRUTH
    assert "active-strategy-count enforcement" in TRUTH


def test_competitive_document_requires_source_and_date_for_claims():
    assert "official URL" in (ROOT / "docs/COMPETITIVE_POSITIONING_AND_MOAT.md").read_text()
    assert "date checked" in (ROOT / "docs/COMPETITIVE_POSITIONING_AND_MOAT.md").read_text()
