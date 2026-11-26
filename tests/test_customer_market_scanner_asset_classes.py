"""Customer market scanner keeps FX and commodity requests allowlisted and read-only."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import main


def _stub_customer_access(monkeypatch):
    class DatabaseSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def get_customer(_authorization, _db, *, require_aal2):
        assert require_aal2 is False
        return SimpleNamespace(id=314), {"sub": "tenant-314"}

    async def require_feature(_db, customer_id, feature):
        assert customer_id == 314
        assert feature == "market_scanner"

    monkeypatch.setattr(main, "SessionLocal", DatabaseSession)
    monkeypatch.setattr(main, "get_customer", get_customer)
    monkeypatch.setattr(main, "_require_feature", require_feature)


def test_forex_scanner_rejects_commodity_and_unknown_pairs_before_fetch(monkeypatch):
    _stub_customer_access(monkeypatch)
    monkeypatch.setattr(
        main.settings,
        "adaptive_established_fx_symbols",
        "EUR_USD,GBP_USD,USD_JPY",
    )
    fetch_started = False

    async def scan(*_args, **_kwargs):
        nonlocal fetch_started
        fetch_started = True
        return []

    monkeypatch.setattr(main, "_scanner_scan_symbols", scan)
    monkeypatch.setattr(
        main,
        "fetch_forex",
        lambda *_args, **_kwargs: pytest.fail("unapproved FX symbol reached provider"),
    )
    monkeypatch.setattr(
        main,
        "fetch_forex_oanda",
        lambda *_args, **_kwargs: pytest.fail("unapproved FX symbol reached OANDA"),
    )

    for symbol in ("XAU_USD", "EUR_CHF", "AAAAAA"):
        with pytest.raises(main.HTTPException) as exc:
            asyncio.run(
                main.customer_market_scanner(
                    asset_class="forex",
                    symbols=symbol,
                    authorization="Bearer tenant-314",
                )
            )
        assert exc.value.status_code == 422

    assert fetch_started is False


def test_forex_scanner_uses_canonical_allowlisted_pairs_and_read_only_fallback(monkeypatch):
    _stub_customer_access(monkeypatch)
    monkeypatch.setattr(main.settings, "adaptive_established_fx_symbols", "EUR_USD,GBP_USD")
    monkeypatch.setattr(main.settings, "forex_broker", "yfinance")
    monkeypatch.setattr(main.settings, "oanda_practice", False)
    monkeypatch.setattr(main.settings, "oanda_account_id", "")
    monkeypatch.setattr(main.settings, "oanda_api_token", "")
    fetched = []

    def fetch_forex(symbol, timeframe, days):
        fetched.append((symbol, timeframe, days))
        return {"source": "test"}

    async def scan(symbols, *, fetch, **_kwargs):
        results = []
        for symbol in symbols:
            fetch(symbol)
            results.append({"symbol": symbol, "score": 0.5})
        return results

    monkeypatch.setattr(main, "fetch_forex", fetch_forex)
    monkeypatch.setattr(main, "_scanner_scan_symbols", scan)

    response = asyncio.run(
        main.customer_market_scanner(
            asset_class="forex",
            symbols="EUR/USD,EUR_USD",
            authorization="Bearer tenant-314",
        )
    )

    assert fetched == [("EUR_USD", "15m", 30)]
    assert response["asset_class"] == "forex"
    assert response["data_source"] == "yfinance_fx"
    assert response["read_only"] is True
    assert [item["symbol"] for item in response["results"]] == ["EUR_USD"]


def test_commodity_scanner_fails_closed_without_oanda_practice_credentials(monkeypatch):
    _stub_customer_access(monkeypatch)
    monkeypatch.setattr(main.settings, "forex_broker", "oanda")
    monkeypatch.setattr(main.settings, "oanda_practice", False)
    monkeypatch.setattr(main.settings, "oanda_account_id", "practice-account")
    monkeypatch.setattr(main.settings, "oanda_api_token", "practice-token")

    async def scan(*_args, **_kwargs):
        pytest.fail("commodity provider must not be called without practice mode")

    monkeypatch.setattr(main, "_scanner_scan_symbols", scan)

    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(
            main.customer_market_scanner(
                asset_class="commodity",
                authorization="Bearer tenant-314",
            )
        )

    assert exc.value.status_code == 503


def test_commodity_scanner_accepts_only_configured_practice_instruments(monkeypatch):
    _stub_customer_access(monkeypatch)
    monkeypatch.setattr(main.settings, "forex_broker", "oanda")
    monkeypatch.setattr(main.settings, "oanda_practice", True)
    monkeypatch.setattr(main.settings, "oanda_account_id", "practice-account")
    monkeypatch.setattr(main.settings, "oanda_api_token", "practice-token")
    monkeypatch.setattr(
        main.settings,
        "adaptive_established_commodity_symbols",
        "XAU_USD,WTICO_USD",
    )
    fetched = []

    def fetch_oanda(symbol, timeframe, days):
        fetched.append((symbol, timeframe, days))
        return {"source": "test"}

    async def scan(symbols, *, fetch, **_kwargs):
        return [{"symbol": symbol, "score": 0.25} for symbol in symbols if fetch(symbol)]

    monkeypatch.setattr(main, "fetch_forex_oanda", fetch_oanda)
    monkeypatch.setattr(main, "_scanner_scan_symbols", scan)

    response = asyncio.run(
        main.customer_market_scanner(
            asset_class="commodity",
            symbols="XAUUSD,WTI",
            authorization="Bearer tenant-314",
        )
    )

    assert fetched == [
        ("XAU_USD", "15m", 30),
        ("WTICO_USD", "15m", 30),
    ]
    assert response["data_source"] == "oanda_practice"
    assert response["read_only"] is True
    assert [item["symbol"] for item in response["results"]] == [
        "XAU_USD",
        "WTICO_USD",
    ]
