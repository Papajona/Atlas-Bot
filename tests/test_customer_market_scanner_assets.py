"""Customer market-scanner asset classes remain read-only and provider-gated."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import main


class _Session:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return False


def _install_customer_gates(monkeypatch):
    async def get_customer(_authorization, _db, *, require_aal2):
        assert require_aal2 is False
        return SimpleNamespace(id=314), {"sub": "customer-314"}

    async def require_feature(_db, customer_id, feature):
        assert customer_id == 314
        assert feature == "market_scanner"

    monkeypatch.setattr(main, "SessionLocal", _Session)
    monkeypatch.setattr(main, "get_customer", get_customer)
    monkeypatch.setattr(main, "_require_feature", require_feature)


def test_forex_scanner_uses_read_only_fx_provider_and_marks_provenance(monkeypatch):
    _install_customer_gates(monkeypatch)
    monkeypatch.setattr(main.settings, "forex_broker", "yfinance", raising=False)
    monkeypatch.setattr(main.settings, "oanda_practice", False, raising=False)
    monkeypatch.setattr(main.settings, "oanda_account_id", "", raising=False)
    monkeypatch.setattr(main.settings, "oanda_api_token", "", raising=False)
    fetched = []
    scanned = {}

    def fetch_forex(symbol, timeframe, days):
        fetched.append((symbol, timeframe, days))
        return object()

    async def scan(symbols, *, fetch, metrics, semaphore, logger):
        scanned["symbols"] = symbols
        for symbol in symbols:
            fetch(symbol)
        return [{"symbol": symbol, "price": 1.25, "score": 0.5} for symbol in symbols]

    monkeypatch.setattr(main, "fetch_forex", fetch_forex)
    monkeypatch.setattr(main, "_scanner_scan_symbols", scan)

    response = asyncio.run(
        main.customer_market_scanner(
            asset_class="forex",
            exchange="yfinance",
            symbols="EUR_USD,GBP_USD",
            authorization="Bearer customer-token",
        )
    )

    assert fetched == [
        ("EUR_USD", "15m", 30),
        ("GBP_USD", "15m", 30),
    ]
    assert response["asset_class"] == "forex"
    assert response["data_source"] == "yfinance_fx"
    assert response["read_only"] is True
    assert [row["data_source"] for row in response["results"]] == [
        "yfinance_fx", "yfinance_fx"
    ]


def test_commodity_scanner_requires_oanda_practice_credentials(monkeypatch):
    _install_customer_gates(monkeypatch)
    monkeypatch.setattr(main.settings, "forex_broker", "yfinance", raising=False)
    monkeypatch.setattr(main.settings, "oanda_practice", False, raising=False)
    monkeypatch.setattr(main.settings, "oanda_account_id", "", raising=False)
    monkeypatch.setattr(main.settings, "oanda_api_token", "", raising=False)

    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(
            main.customer_market_scanner(
                asset_class="commodity",
                symbols="XAU_USD",
                authorization="Bearer customer-token",
            )
        )
    assert exc.value.status_code == 503


def test_commodity_scanner_uses_only_allowlisted_oanda_practice_instruments(monkeypatch):
    _install_customer_gates(monkeypatch)
    monkeypatch.setattr(main.settings, "forex_broker", "oanda", raising=False)
    monkeypatch.setattr(main.settings, "oanda_practice", True, raising=False)
    monkeypatch.setattr(main.settings, "oanda_account_id", "practice-account", raising=False)
    monkeypatch.setattr(main.settings, "oanda_api_token", "practice-token", raising=False)
    fetched = []

    def fetch_oanda(symbol, timeframe, days):
        fetched.append((symbol, timeframe, days))
        return object()

    async def scan(symbols, *, fetch, metrics, semaphore, logger):
        for symbol in symbols:
            fetch(symbol)
        return [{"symbol": symbol, "price": 2000.0, "score": 0.1} for symbol in symbols]

    monkeypatch.setattr(main, "fetch_forex_oanda", fetch_oanda)
    monkeypatch.setattr(main, "_scanner_scan_symbols", scan)

    response = asyncio.run(
        main.customer_market_scanner(
            asset_class="commodity",
            symbols="XAU_USD,XAG_USD",
            authorization="Bearer customer-token",
        )
    )
    assert fetched == [
        ("XAU_USD", "15m", 30),
        ("XAG_USD", "15m", 30),
    ]
    assert response["data_source"] == "oanda_practice"
    assert response["read_only"] is True


def test_commodity_scanner_rejects_unverified_symbols_before_fetch(monkeypatch):
    _install_customer_gates(monkeypatch)
    monkeypatch.setattr(main.settings, "forex_broker", "oanda", raising=False)
    monkeypatch.setattr(main.settings, "oanda_practice", True, raising=False)
    monkeypatch.setattr(main.settings, "oanda_account_id", "practice-account", raising=False)
    monkeypatch.setattr(main.settings, "oanda_api_token", "practice-token", raising=False)

    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(
            main.customer_market_scanner(
                asset_class="commodity",
                symbols="UNKNOWN_USD",
                authorization="Bearer customer-token",
            )
        )
    assert exc.value.status_code == 422
