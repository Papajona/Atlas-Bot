from pathlib import Path
from app.grid_trading import build_grid, grid_profit_pct

ROOT=Path(__file__).resolve().parents[1]

def test_geometric_grid_is_monotonic():
    levels=build_grid(100,200,10)
    assert len(levels)==11
    assert all(levels[i].price < levels[i+1].price for i in range(10))

def test_arithmetic_grid_is_even():
    levels=build_grid(100,200,10,True)
    assert round(levels[1].price-levels[0].price,8)==10
    assert round(levels[-1].price,8)==200

def test_main_has_webhook_and_connector_routes():
    text=(ROOT/'app'/'main.py').read_text()
    assert '/api/webhooks/{endpoint_id}' in text
    assert '/api/customer/connectors' in text
    assert 'RECEIVED_ONLY' in text

def test_migration_revision():
    text=(ROOT/'alembic'/'versions'/'0015_grid_webhooks_connectors.py').read_text()
    assert '0015_grid_webhooks_connectors' in text
    assert '0014_trading_product_features' in text


def test_grid_start_is_fail_closed_until_execution_controller_exists():
    source = (ROOT / "app" / "main.py").read_text()
    route = source.split('@app.post("/api/customer/grid-bots/{bot_id}/action")', 1)[1].split(
        '@app.post("/api/customer/broker/deriv/connect")', 1
    )[0]
    assert 'if req.action == "START":' in route
    assert 'raise HTTPException(409, "Grid execution controller is not enabled; this configuration is paper-only")' in route
