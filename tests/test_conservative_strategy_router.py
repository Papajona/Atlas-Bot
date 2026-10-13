import numpy as np
import pandas as pd

from app.strategy_router import _periods_per_year, classify_regime


def test_hourly_annualization_uses_bar_cadence():
    hourly = pd.date_range("2025-01-01", periods=100, freq="h", tz="UTC")
    crypto = _periods_per_year(hourly, "crypto")
    forex = _periods_per_year(hourly, "forex")
    equities = _periods_per_year(hourly, "stocks")
    assert crypto == 365.25 * 24
    assert forex == 252 * 24
    assert equities == 252 * 6.5


def test_daily_annualization_uses_market_calendar():
    daily = pd.date_range("2025-01-01", periods=100, freq="D", tz="UTC")
    assert _periods_per_year(daily, "crypto") == 365.25
    assert _periods_per_year(daily, "forex") == 252
    assert _periods_per_year(daily, "commodity") == 252  # daily bars are one observation per active trading day


def test_regime_classifier_is_finite_and_aligned_for_hourly_data():
    idx = pd.date_range("2025-01-01", periods=400, freq="h", tz="UTC")
    close = 100 * np.exp(np.cumsum(np.full(len(idx), 0.0001)))
    frame = pd.DataFrame({"close": close}, index=idx)
    regime = classify_regime(frame, asset="forex")
    assert regime.index.equals(frame.index)
    assert len(regime) == len(frame)
    assert regime.notna().all()
