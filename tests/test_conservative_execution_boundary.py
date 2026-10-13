import ast
from pathlib import Path

import pytest

from app.execution import RiskBlocked, execute_signal


ROOT = Path(__file__).resolve().parents[1]


def test_conservative_profile_is_blocked_until_trusted_metadata_is_wired():
    source = (ROOT / "app/execution.py").read_text()
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "execute_signal")
    block = ast.get_source_segment(source, function) or ""
    assert 'if strategy == "atlas_conservative_v1":' in block
    assert "trusted instrument metadata and " in block
    assert 'signal.get("instrument_metadata")' not in block


@pytest.mark.asyncio
async def test_conservative_profile_never_accepts_client_metadata_as_authority():
    # The current central path deliberately fails closed before broker submission:
    # signal payload metadata is user-controlled and is not proof of contract specs.
    with pytest.raises(RiskBlocked, match="trusted instrument metadata"):
        await execute_signal(
            symbol="BTC/USDT",
            side="buy",
            quantity=0.01,
            price=50000,
            signal={
                "strategy": "atlas_conservative_v1",
                "instrument_metadata": {
                    "contract_multiplier": 1,
                    "quote_to_account_rate": 1,
                    "amount_step": 0.001,
                    "min_amount": 0.001,
                    "max_amount": 1000,
                    "price_tick": 0.01,
                },
            },
            exchange="binance",
            timeframe="1h",
            signal_timestamp="2026-10-13T00:00:00+00:00",
            force_paper=True,
            stop_loss_price=49000,
            take_profit_price=52000,
        )
