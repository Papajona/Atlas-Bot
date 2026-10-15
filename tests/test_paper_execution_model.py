import numpy as np
import pandas as pd
import pytest

from app.paper_execution_model import (
    adverse_fill_price,
    estimate_paper_slippage,
    paper_execution_context_from_ohlcv,
)


def _context(*, volume=1000.0, volatility=0.01):
    return {
        "source": "ccxt_ohlcv",
        "bar_volume": volume,
        "volume_unit": "base_asset",
        "realized_volatility_per_bar": volatility,
        "spread_bps": 4.0,
    }


def test_paper_slippage_increases_with_order_size_and_realized_volatility():
    small = estimate_paper_slippage(
        side="buy", quantity=1.0, base_slippage_bps=2.0, context=_context()
    )
    large = estimate_paper_slippage(
        side="buy", quantity=100.0, base_slippage_bps=2.0, context=_context()
    )
    volatile = estimate_paper_slippage(
        side="buy", quantity=100.0, base_slippage_bps=2.0,
        context=_context(volatility=0.04),
    )

    assert small["status"] == "DYNAMIC_IMPACT_ESTIMATE"
    assert small["calibration_status"] == "UNVALIDATED_HEURISTIC"
    assert small["impact_inputs_complete"] is True
    assert small["half_spread_bps"] == pytest.approx(2.0)
    assert large["market_impact_bps"] > small["market_impact_bps"]
    assert volatile["market_impact_bps"] > large["market_impact_bps"]
    assert large["modeled_slippage_bps"] > small["modeled_slippage_bps"]


def test_paper_slippage_marks_missing_or_untrusted_volume_as_baseline_only():
    missing = estimate_paper_slippage(
        side="sell",
        quantity=5.0,
        base_slippage_bps=2.0,
        context={"spread_bps": 6.0, "volume_unit": "tick_count",
                 "bar_volume": 1000.0, "realized_volatility_per_bar": 0.02},
    )
    no_context = estimate_paper_slippage(
        side="buy", quantity=1.0, base_slippage_bps=2.0, context=None
    )

    assert missing["status"] == "BASELINE_ONLY_MISSING_VERIFIED_IMPACT_INPUTS"
    assert missing["market_impact_bps"] is None
    assert missing["modeled_slippage_bps"] == pytest.approx(5.0)
    assert no_context["modeled_slippage_bps"] == pytest.approx(2.0)
    assert no_context["impact_inputs_complete"] is False


def test_paper_context_only_uses_ccxt_crypto_volume_as_base_asset_quantity():
    index = pd.date_range("2026-01-01", periods=30, freq="h", tz="UTC")
    close = 100.0 * np.cumprod(1.0 + np.sin(np.arange(30)) * 0.001)
    frame = pd.DataFrame({
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": np.full(30, 250.0),
    }, index=index)
    frame.attrs["data_provenance"] = {"source": "ccxt_ohlcv"}

    crypto = paper_execution_context_from_ohlcv(frame, asset="crypto", spread_bps=8.0)
    oanda = frame.copy()
    oanda.attrs["data_provenance"] = {"source": "oanda_v20_mid"}
    forex = paper_execution_context_from_ohlcv(oanda, asset="forex", spread_bps=2.0)

    assert crypto["volume_unit"] == "base_asset"
    assert crypto["bar_volume"] == pytest.approx(250.0)
    assert crypto["realized_volatility_per_bar"] is not None
    assert crypto["spread_bps"] == pytest.approx(8.0)
    assert forex["volume_unit"] is None
    assert forex["bar_volume"] is None
    assert forex["realized_volatility_per_bar"] is not None
    # Price-count volume from the OANDA adapter is not fungible with base-asset quantity.
    assert forex["source"] == "oanda_v20_mid"


def test_paper_impact_is_capped_and_fill_price_is_adverse_for_both_sides():
    estimate = estimate_paper_slippage(
        side="buy", quantity=1_000_000.0, base_slippage_bps=2.0,
        context=_context(volume=1.0, volatility=2.0),
        impact_coefficient=100.0, max_market_impact_bps=100.0,
    )
    assert estimate["market_impact_bps"] == pytest.approx(100.0)
    assert estimate["modeled_slippage_bps"] == pytest.approx(104.0)
    assert adverse_fill_price(100.0, "buy", 10.0) == pytest.approx(100.1)
    assert adverse_fill_price(100.0, "sell", 10.0) == pytest.approx(99.9)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"side": "hold", "quantity": 1.0, "base_slippage_bps": 2.0, "context": None},
        {"side": "buy", "quantity": 0.0, "base_slippage_bps": 2.0, "context": None},
        {"side": "buy", "quantity": 1.0, "base_slippage_bps": float("nan"), "context": None},
        {"side": "buy", "quantity": 1.0, "base_slippage_bps": 2.0,
         "context": _context(volume=0.0, volatility=0.01)},
    ],
)
def test_paper_impact_model_rejects_invalid_inputs(kwargs):
    if kwargs.get("context") and kwargs["context"].get("volume_unit") == "base_asset":
        if kwargs["context"].get("bar_volume") == 0.0:
            with pytest.raises(ValueError):
                estimate_paper_slippage(**kwargs)
            return
    with pytest.raises(ValueError):
        estimate_paper_slippage(**kwargs)


@pytest.mark.parametrize(
    "side, reference, slippage",
    [
        ("buy", float("nan"), 2.0),
        ("buy", 100.0, -1.0),
        ("short", 100.0, 2.0),
    ],
)
def test_adverse_fill_price_rejects_invalid_inputs(side, reference, slippage):
    with pytest.raises(ValueError):
        adverse_fill_price(reference, side, slippage)
