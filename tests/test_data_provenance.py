import pandas as pd

from app.data import _attach_data_provenance


def test_market_data_provenance_hash_is_deterministic():
    idx = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    df = pd.DataFrame(
        {"open": [1.0, 2.0, 3.0], "high": [1.1, 2.1, 3.1],
         "low": [0.9, 1.9, 2.9], "close": [1.0, 2.0, 3.0],
         "volume": [10.0, 20.0, 30.0]},
        index=idx,
    )
    a = _attach_data_provenance(df.copy(), source="test", symbol="X", exchange="test",
                                 timeframe="1h", fetched_at="2026-01-01T00:00:00+00:00")
    b = _attach_data_provenance(df.copy(), source="test", symbol="X", exchange="test",
                                 timeframe="1h", fetched_at="2026-01-01T00:00:00+00:00")
    assert a.attrs["data_provenance"]["sha256"] == b.attrs["data_provenance"]["sha256"]
    assert len(a.attrs["data_provenance"]["sha256"]) == 64
