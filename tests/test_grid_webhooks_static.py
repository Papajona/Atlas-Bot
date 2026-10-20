from pathlib import Path
from app.grid_trading import build_grid

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


def test_grid_start_endpoint_returns_conflict_without_execution_controller(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from fastapi import HTTPException
    import app.main as main_module

    class FakeResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(id=7, customer_id=11, status="STOPPED", mode="PAPER")

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return FakeResult()

    async def fake_get_customer(*_args, **_kwargs):
        return SimpleNamespace(id=11), {}

    async def fake_require_feature(*_args, **_kwargs):
        return None

    async def exercise():
        monkeypatch.setattr(main_module, "SessionLocal", lambda: FakeDB())
        monkeypatch.setattr(main_module, "get_customer", fake_get_customer)
        monkeypatch.setattr(main_module, "_require_feature", fake_require_feature)
        try:
            await main_module.grid_bot_action(
                7, main_module.BotActionRequest(action="START"), authorization="Bearer test"
            )
        except HTTPException as exc:
            assert exc.status_code == 409
            assert "paper-only" in str(exc.detail)
        else:
            raise AssertionError("grid START must fail closed without an execution controller")

    asyncio.run(exercise())
