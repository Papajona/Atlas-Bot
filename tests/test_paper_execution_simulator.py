import numpy as np
import pandas as pd
import pytest

from app.paper_execution import (
    PaperExecutionError,
    build_paper_market_context,
    simulate_paper_order,
    validate_paper_context,
)


def candles(*, volume=100.0, noisy=False, n=25):
    index = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    if noisy:
        changes = np.asarray([0.012, -0.009, 0.015, -0.013, 0.008] * 5, dtype=float)
        close = 100.0 * np.cumprod(1.0 + changes[:n])
    else:
        close = 100.0 + np.arange(n, dtype=float) * 0.01
    return pd.DataFrame({
        "open": close,
        "high": close * 1.002,
        "low": close * 0.998,
        "close": close,
        "volume": np.full(n, volume, dtype=float),
    }, index=index)


def make_context(*, noisy=False, volume=100.0):
    df = candles(noisy=noisy, volume=volume)
    context = build_paper_market_context(df, symbol="BTC/USDT", timeframe="1h")
    return df, context


def simulate(context, *, order_id="paper-test", quantity=0.1, **overrides):
    params = {
        "order_id": order_id,
        "side": "buy",
        "quantity": quantity,
        "price": context["last_close"],
        "base_slippage_bps": 2.0,
        "taker_fee_bps": 5.5,
        "context": context,
        "max_volume_participation": 0.01,
        "volatility_slippage_multiplier": 0.25,
        "market_impact_bps": 10.0,
        "rejection_rate": 0.0,
        "latency_min_ms": 250,
        "latency_max_ms": 250,
    }
    params.update(overrides)
    return simulate_paper_order(**params)


def test_context_only_uses_crypto_base_volume_not_fx_tick_counts():
    df = candles()
    with pytest.raises(PaperExecutionError, match="base-asset-volume"):
        build_paper_market_context(df, symbol="EUR_USD", timeframe="1h", asset="forex")


def test_context_requires_timezone_monotonic_index_and_enough_closed_bars():
    df = candles(n=8)
    with pytest.raises(PaperExecutionError, match="At least"):
        build_paper_market_context(df, symbol="BTC/USDT", timeframe="1h")
    df = candles()
    df.index = df.index.tz_localize(None)
    with pytest.raises(PaperExecutionError, match="timezone-aware"):
        build_paper_market_context(df, symbol="BTC/USDT", timeframe="1h")


def test_context_fails_closed_on_later_timestamp_or_bad_reference_price():
    df, context = make_context()
    before_context = (df.index[-1] - pd.Timedelta(seconds=1)).isoformat()
    with pytest.raises(PaperExecutionError, match="after the decision"):
        validate_paper_context(
            context, symbol="BTC/USDT", timeframe="1h",
            signal_timestamp=before_context, price=context["last_close"],
        )
    with pytest.raises(PaperExecutionError, match="deviates too far"):
        validate_paper_context(
            context, symbol="BTC/USDT", timeframe="1h",
            signal_timestamp=df.index[-1].isoformat(), price=context["last_close"] * 1.02,
        )


def test_slippage_increases_with_volatility_and_order_participation():
    _, calm = make_context(noisy=False)
    _, volatile = make_context(noisy=True)
    calm_result = simulate(calm, order_id="same-order", quantity=0.1)
    volatile_result = simulate(volatile, order_id="same-order", quantity=0.1)
    large_result = simulate(calm, order_id="same-order", quantity=0.5)

    assert volatile_result["volatility_slippage_bps"] > calm_result["volatility_slippage_bps"]
    assert volatile_result["total_slippage_bps"] > calm_result["total_slippage_bps"]
    assert large_result["market_impact_slippage_bps"] > calm_result["market_impact_slippage_bps"]
    assert calm_result["fidelity"] == "SYNTHETIC_OHLCV_PROXY_NOT_VENUE_CALIBRATED"
    assert calm_result["promotion_grade_execution_evidence"] is False


def test_volume_cap_produces_partial_ioc_fill_and_cancels_remainder():
    _, context = make_context(volume=100.0)
    result = simulate(context, order_id="large-order", quantity=10.0)

    assert result["status"] == "CANCELED"
    assert result["reason"] == "ioc_remainder_canceled_by_volume_cap"
    assert result["filled_quantity"] == pytest.approx(1.0)
    assert result["remaining_quantity"] == pytest.approx(9.0)
    assert result["fee"] > 0


def test_rejection_is_deterministic_and_does_not_book_a_fill():
    _, context = make_context()
    first = simulate(context, order_id="deterministic-reject", quantity=0.1, rejection_rate=1.0)
    second = simulate(context, order_id="deterministic-reject", quantity=0.1, rejection_rate=1.0)

    assert first == second
    assert first["status"] == "REJECTED"
    assert first["filled_quantity"] == 0.0
    assert first["remaining_quantity"] == pytest.approx(0.1)
    assert first["fee"] == 0.0


def test_latency_and_rejection_outcomes_are_repeatable_for_an_order_id():
    _, context = make_context()
    a = simulate(context, order_id="stable-order", quantity=0.1, latency_min_ms=100, latency_max_ms=500)
    b = simulate(context, order_id="stable-order", quantity=0.1, latency_min_ms=100, latency_max_ms=500)
    assert a == b
    assert 100 <= a["latency_ms"] <= 500
