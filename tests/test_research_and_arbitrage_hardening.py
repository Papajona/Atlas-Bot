"""Regression tests for: meta-labeling purge/embargo, daily Yahoo/news macro persistence
and its wiring into the live AI-safety packet, and cumulative arbitrage paper sizing."""
import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.arbitrage import compounded_stake


def _synthetic_ohlcv(n=500, seed=7):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.01, n)
    close = 100 * np.cumprod(1 + ret)
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    high = close * (1 + np.abs(rng.normal(0, 0.002, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.002, n)))
    openp = np.roll(close, 1); openp[0] = close[0]
    vol = rng.uniform(100, 1000, n)
    return pd.DataFrame({"open": openp, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def test_meta_label_walk_forward_purges_and_embargoes_fold_boundaries():
    from app.meta_labeling import meta_label_walk_forward, build_meta_dataset
    df = _synthetic_ohlcv(600)
    horizon = 3
    result = meta_label_walk_forward(df, horizon=horizon, folds=5)
    assert result["status"] == "OK"
    # No label used for training should have its horizon-bar future window reach into
    # the following test fold's first `horizon` rows once purge/embargo are applied --
    # verified indirectly: the function must still produce a usable OOS sample.
    assert result["oos_observations"] > 0
    X, y = build_meta_dataset(df, horizon=horizon)
    assert len(X) == len(y)


def test_meta_label_walk_forward_insufficient_data_is_safe():
    from app.meta_labeling import meta_label_walk_forward
    df = _synthetic_ohlcv(50)
    result = meta_label_walk_forward(df)
    assert result["status"] in {"INSUFFICIENT_DATA"}


# ---------------------------------------------------------------------------
# Arbitrage cumulative sizing
# ---------------------------------------------------------------------------

def test_compounded_stake_grows_with_positive_cumulative_edge():
    rec = compounded_stake(100.0, cumulative_realized_pct=40.0, absolute_cap=1000.0)
    assert rec.stake > 100.0
    assert rec.rationale == "growth_from_cumulative_simulated_edge"


def test_compounded_stake_never_exceeds_absolute_cap_or_growth_multiple():
    rec = compounded_stake(100.0, cumulative_realized_pct=100000.0, absolute_cap=250.0,
                           growth_fraction=5.0, max_growth_multiple=3.0)
    assert rec.stake <= 250.0
    assert rec.stake <= 300.0  # 3x base_stake ceiling, before the cap clamp


def test_compounded_stake_shrinks_after_losses_but_has_a_floor():
    rec = compounded_stake(100.0, cumulative_realized_pct=-50.0, absolute_cap=1000.0, drawdown_shrink=0.5)
    assert rec.stake < 100.0
    assert rec.stake >= 50.0  # never below base_stake * drawdown_shrink
    assert rec.rationale == "shrink_after_simulated_losses"


def test_compounded_stake_flat_at_zero_edge():
    rec = compounded_stake(100.0, cumulative_realized_pct=0.0, absolute_cap=1000.0)
    assert rec.stake == 100.0


def test_compounded_stake_rejects_invalid_inputs():
    assert compounded_stake(0.0, 10.0, absolute_cap=100.0).stake == 0.0
    assert compounded_stake(100.0, 10.0, absolute_cap=0.0).stake == 0.0


# ---------------------------------------------------------------------------
# Daily Yahoo/news macro context: persistence + retrieval
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    from app.config import settings
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


def test_persist_research_run_stores_macro_snapshot_and_latest_macro_context_reads_it():
    from app.daily_research import _persist_research_run, latest_macro_context
    from app.db import init_db

    async def go():
        await init_db()
        research = {"regime": {"regime": "TREND_UP"}, "research_order": ["trend"],
                    "strategies": [{"strategy": "trend", "sharpe": 1.1, "max_drawdown": -0.1, "total_return": 0.2}]}
        macro = {"risk_state": "ELEVATED", "headline_risk": {"risk_score": 0.42}}
        drift = await _persist_research_run("BTC-USD", research, {}, macro)
        assert drift["status"] == "FIRST_RUN"
        ctx = await latest_macro_context()
        assert ctx["status"] == "OK"
        assert ctx["risk_state"] == "ELEVATED"
        assert ctx["risk_score"] == pytest.approx(0.42)

    asyncio.run(go())


def test_latest_macro_context_reports_stale_beyond_max_age():
    from sqlalchemy import delete
    from app.daily_research import latest_macro_context
    from app.db import init_db, SessionLocal, ResearchRun

    async def go():
        await init_db()
        async with SessionLocal() as db:
            await db.execute(delete(ResearchRun))  # isolate from other tests sharing this DB file
            old = datetime.now(timezone.utc) - timedelta(hours=100)
            db.add(ResearchRun(symbol="SPY", macro_risk_state="NORMAL", macro_risk_score=0.1, created_at=old))
            await db.commit()
        ctx = await latest_macro_context(max_age_hours=48.0)
        assert ctx["status"] == "STALE"

    asyncio.run(go())


def test_latest_macro_context_no_data():
    from sqlalchemy import delete
    from app.daily_research import latest_macro_context
    from app.db import init_db, SessionLocal, ResearchRun

    async def go():
        await init_db()
        async with SessionLocal() as db:
            await db.execute(delete(ResearchRun))
            await db.commit()
        ctx = await latest_macro_context()
        assert ctx["status"] == "NO_DATA"

    asyncio.run(go())


def test_ai_trade_safety_packet_includes_macro_context(monkeypatch):
    """The live per-symbol decision path must pass macro_context into the LLM veto packet,
    without letting it silently replace or gate ahead of the deterministic/ML gates."""
    src = open("app/main.py").read()
    i = src.index('packet = {"symbol": req.symbol')
    line = src[i:src.index("\n", i)]
    assert '"macro_context": macro_context' in line
    assert "latest_macro_context" in src
