from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app import trade_learning


def test_replay_counts_adjacent_regime_transitions_without_strict_zip_failure(monkeypatch):
    index = pd.date_range("2026-01-01T00:00:00Z", periods=6, freq="h")
    df = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0, 106.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0],
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
        },
        index=index,
    )

    signals = pd.DataFrame(
        {name: np.full(len(df), 0.2) for name in trade_learning.STRATEGIES},
        index=index,
    )
    signals["realized_vol"] = 0.1
    regimes = pd.Series(["A", "A", "B", "B", "C", "C"], index=index)
    monkeypatch.setattr(trade_learning, "strategy_signals", lambda *_args, **_kwargs: signals)
    monkeypatch.setattr(trade_learning, "classify_regime", lambda *_args, **_kwargs: regimes)

    episode = SimpleNamespace(
        asset="crypto",
        timeframe="1h",
        entry_at=datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc),
        exit_at=datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc),
        entry_price=102.0,
        exit_price=105.0,
        entry_quantity=1.0,
        side="buy",
        net_pnl=3.0,
        strategy=trade_learning.STRATEGIES[0],
    )

    result = trade_learning.replay_trade_episode(df, episode, costs_bps=1.0)

    # Regime path spans indices 1..4: A, B, B, C => two adjacent transitions.
    assert result["regime_transitions"] == 2
    assert result["market_flow"]["regime_transitions"] == 2
    assert result["bar_alignment"]["completed_candles_only"] is True
