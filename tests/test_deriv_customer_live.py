import asyncio
import ast
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.deriv import DerivBroker, DerivConfig, DerivError


def test_deriv_live_requires_account_id():
    try:
        DerivBroker(DerivConfig(123, 'x'*24, live=True))
    except DerivError as exc:
        assert 'account ID' in str(exc)
    else:
        raise AssertionError('live Deriv broker accepted missing account ID')


def test_deriv_uses_current_otp_account_path():
    b = DerivBroker(DerivConfig(123, 'x'*24, 'CR123'))
    assert b.config.otp_endpoint.endswith('/trading/v1/options/accounts')


def test_main_has_customer_scoped_deriv_routes_without_duplicates():
    source = Path(__file__).parents[1] / 'app' / 'main.py'
    tree = ast.parse(source.read_text())
    routes=[]
    for n in ast.walk(tree):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and isinstance(d.func.value,ast.Name) and d.func.value.id=='app' and d.args and isinstance(d.args[0],ast.Constant):
                    routes.append((d.func.attr,d.args[0].value))
    assert ('post','/api/customer/broker/deriv/connect') in routes
    assert ('post','/api/customer/deriv/preflight') in routes
    assert ('post','/api/customer/deriv/execute') in routes
    assert len(routes) == len(set(routes))


def test_deriv_practice_preflight_is_read_only_and_fail_closed_for_execution():
    broker = DerivBroker(DerivConfig(123, "x" * 24, account_id="CR123", live=False))
    calls = []

    async def fake_call(payload, *, authenticated=True):
        calls.append((payload, authenticated))
        req = payload.get("req_id")
        if payload.get("authorize") == broker.config.token:
            return {"authorize": {"loginid": "CR123", "currency": "USD", "scopes": ["read", "trade"]}}
        if payload.get("balance") == 1:
            return {"balance": {"balance": "100.00", "currency": "USD"}}
        if payload.get("active_symbols") == "brief":
            return {"active_symbols": [{"symbol": "R_100", "market": "synthetic_index"}]}
        raise AssertionError(f"unexpected Deriv request: {payload}")

    async def run():
        with patch.object(broker, "_call", new=AsyncMock(side_effect=fake_call)):
            result = await broker.preflight()
            assert result["connected"] is True
            assert result["account_id"] == "CR123"
            assert result["currency"] == "USD"
            assert result["balance"] == "100.00"
            assert result["symbols_available"] == 1
            assert result["trade_scope"] is True
            assert result["live_execution_enabled"] is False
            assert result["execution_authority"] is False

            try:
                await broker.buy("proposal-1", 1.0)
            except DerivError as exc:
                assert str(exc) == "Deriv live execution is disabled"
            else:
                raise AssertionError("Deriv practice broker allowed a live buy")

            try:
                await broker.sell("contract-1", 1.0)
            except DerivError as exc:
                assert str(exc) == "Deriv live execution is disabled"
            else:
                raise AssertionError("Deriv practice broker allowed a live sell")

    asyncio.run(run())
    assert [x[0].get("req_id") for x in calls] == [1, 2, 3]
    assert [authenticated for _, authenticated in calls] == [False, True, False]
