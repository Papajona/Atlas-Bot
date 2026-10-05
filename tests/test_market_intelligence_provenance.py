import sys
import types

import pandas as pd

from app.market_intelligence import yahoo_history


def test_yahoo_history_attaches_research_provenance(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": [1.0, 2.0, 3.0],
            "High": [1.1, 2.1, 3.1],
            "Low": [0.9, 1.9, 2.9],
            "Close": [1.0, 2.0, 3.0],
            "Volume": [10.0, 20.0, 30.0],
        },
        index=idx,
    )

    fake_yf = types.SimpleNamespace(
        download=lambda *args, **kwargs: frame.copy()
    )
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    out = yahoo_history("BTC-USD", period="3y", interval="1d")
    provenance = out.attrs["data_provenance"]

    assert provenance["source"] == "yahoo_finance"
    assert provenance["symbol"] == "BTC-USD"
    assert provenance["timeframe"] == "1d"
    assert len(provenance["sha256"]) == 64
