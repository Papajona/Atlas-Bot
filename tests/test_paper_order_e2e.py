"""End-to-end paper order through execute_signal: catches NameErrors and Decimal/float TypeErrors."""
import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path

os.environ.setdefault("ENVIRONMENT", "development")

import pytest
from sqlalchemy import select



def _configure_test_calibration(monkeypatch, directory, exchange, symbol):
    from app.config import settings
    from app.trading_core import PROFILES

    evidence = {
        "schema": "atlas-paper-execution-calibration-v1",
        "fixture_only": True,
        "provenance_source": "synthetic-unit-test-data",
        "calibration_method": "deterministic-unit-test-fixture-not-venue-calibration",
        "dataset_sha256": "a" * 64,
        "observation_counts": {"fills": 10, "latency": 10, "order_outcomes": 10},
        "supported_markets": [f"{exchange.lower()}:{symbol}".lower()],
        "test_parameter_sets": [{
            "base_slippage_bps": float(PROFILES["crypto"].slippage_bps),
            "max_participation_rate": float(settings.paper_impact_max_participation_rate),
            "impact_coefficient": float(settings.paper_impact_coefficient),
            "rejection_probability": float(settings.paper_execution_rejection_probability),
            "latency_min_ms": float(settings.paper_execution_latency_min_ms),
            "latency_median_ms": float(settings.paper_execution_latency_median_ms),
            "latency_p95_ms": float(settings.paper_execution_latency_p95_ms),
            "latency_max_ms": float(settings.paper_execution_latency_max_ms),
            "latency_impact_coefficient": float(settings.paper_latency_impact_coefficient),
        }],
    }
    artifact = Path(directory) / f"paper-calibration-{uuid.uuid4().hex}.json"
    artifact.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    monkeypatch.setattr(settings, "paper_execution_evidence_source", "synthetic-unit-test-fixture")
    monkeypatch.setattr(settings, "paper_execution_evidence_path", str(artifact))
    monkeypatch.setattr(settings, "paper_execution_evidence_id", digest)
    return artifact, digest


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    import hashlib
    from pathlib import Path
    from cryptography.fernet import Fernet
    from app.config import settings
    calibration_artifact = Path(__file__).parent / "fixtures" / "paper_execution_calibration_test.json"
    calibration_id = "sha256:" + hashlib.sha256(calibration_artifact.read_bytes()).hexdigest()
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")
    monkeypatch.setattr(settings, "discipline_entry_cooldown_seconds", 0)
    monkeypatch.setattr(settings, "paper_impact_max_participation_rate", 0.10)
    monkeypatch.setattr(settings, "paper_impact_coefficient", 0.5)
    monkeypatch.setattr(settings, "paper_execution_rejection_probability", 0.0)
    monkeypatch.setattr(settings, "paper_execution_latency_min_ms", 10.0)
    monkeypatch.setattr(settings, "paper_execution_latency_median_ms", 20.0)
    monkeypatch.setattr(settings, "paper_execution_latency_p95_ms", 50.0)
    monkeypatch.setattr(settings, "paper_execution_latency_max_ms", 100.0)
    monkeypatch.setattr(settings, "paper_latency_impact_coefficient", 0.1)
    monkeypatch.setattr(settings, "paper_execution_evidence_source", "unit-test-calibration-fixture")
    monkeypatch.setattr(settings, "paper_execution_evidence_path", str(calibration_artifact))
    monkeypatch.setattr(settings, "paper_execution_evidence_id", calibration_id)


def test_paper_buy_then_sell_round_trip(monkeypatch, tmp_path):
    from app import execution
    from app.db import init_db, SessionLocal, Trade, Position, AuditLog

    sym = f"PAPERTEST-{uuid.uuid4().hex[:8]}/USDT"
    _configure_test_calibration(monkeypatch, tmp_path, "binance", sym)
    ts = uuid.uuid4().hex  # unique signal timestamp => unique idempotent client order id

    async def run():
        await init_db()
        market_context = {
            "tradable_volume_units": 10.0,
            "spread_bps": 2.0,
            "recent_volatility_bps": 30.0,
            "bar_duration_ms": 3_600_000,
            "exchange": "binance",
            "symbol": sym,
            "market_type": "spot",
        }
        buy = await execution.execute_signal(
            sym, "buy", 0.01, 50000.0,
            {"score": 0.9, "paper_market_context": market_context},
            "binance", "1h", f"{ts}-b", force_paper=True,
            stop_loss_price=49000.0, take_profit_price=52000.0,
        )
        assert buy, buy
        sell = await execution.execute_signal(
            sym, "sell", 0.01, 50500.0,
            {"score": 0.9, "paper_market_context": market_context},
            "binance", "1h", f"{ts}-s", force_paper=True,
            stop_loss_price=51500.0, take_profit_price=49000.0,
        )
        assert sell, sell
        async with SessionLocal() as db:
            trades = (await db.execute(select(Trade).where(Trade.id.in_([buy["trade_id"], sell["trade_id"]])))).scalars().all()
            assert len(trades) == 2
            assert all(t.status == "SIMULATED" for t in trades)
            assert all(isinstance(t.filled_quantity, float) and isinstance(t.requested_price, float) for t in trades)
            assert all(t.filled_quantity == t.requested_quantity for t in trades)
            assert all("VOLUME_PARTICIPATION_VOLATILITY_LATENCY_REJECTION_V2" in (t.reason or "") for t in trades)
            assert all("simulated_latency_ms" in (t.reason or "") for t in trades)
            assert all("simulated_rejection" in (t.reason or "") for t in trades)
            positions = (await db.execute(select(Position).where(Position.symbol == sym, Position.exchange == "binance"))).scalars().all()
            assert len(positions) == 1
            assert abs(float(positions[0].quantity)) < 1e-12
            audits = (await db.execute(select(AuditLog).where(AuditLog.event == "PAPER_ORDER"))).scalars().all()
            assert audits

    asyncio.run(run())


def test_calibrated_simulated_venue_rejection_books_no_position(monkeypatch, tmp_path):
    from app import execution
    from app.config import settings
    from app.db import init_db, SessionLocal, Trade, Position

    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "paper_execution_rejection_probability", 1.0)
    sym = f"PAPERREJECT-{uuid.uuid4().hex[:8]}/USDT"
    _configure_test_calibration(monkeypatch, tmp_path, "binance", sym)
    ts = uuid.uuid4().hex
    context = {
        "tradable_volume_units": 10.0,
        "spread_bps": 2.0,
        "recent_volatility_bps": 30.0,
        "bar_duration_ms": 3_600_000,
        "exchange": "binance",
        "symbol": sym,
        "market_type": "spot",
    }

    async def run():
        await init_db()
        result = await execution.execute_signal(
            sym, "buy", 0.01, 50000.0,
            {"score": 0.9, "paper_market_context": context},
            "binance", "1h", f"{ts}-b", force_paper=True,
            stop_loss_price=49000.0, take_profit_price=52000.0,
        )
        assert result["status"] == "REJECTED"
        assert result["paper_execution"]["simulated_rejection"] is True
        async with SessionLocal() as db:
            trade = (await db.execute(select(Trade).where(Trade.id == result["trade_id"]))).scalar_one()
            assert trade.status == "REJECTED"
            assert trade.filled_quantity == 0.0
            assert trade.remaining_quantity == 0.0
            positions = (await db.execute(select(Position).where(Position.symbol == sym, Position.exchange == "binance"))).scalars().all()
            assert positions == []

    asyncio.run(run())


def test_staging_paper_order_is_rejected_without_calibrated_market_context(monkeypatch):
    from app import execution
    from app.config import settings
    from app.db import init_db, SessionLocal, Trade, Position

    monkeypatch.setattr(settings, "environment", "staging")
    sym = f"PAPERBLOCK-{uuid.uuid4().hex[:8]}/USDT"
    ts = uuid.uuid4().hex

    async def run():
        await init_db()
        result = await execution.execute_signal(
            sym, "buy", 0.01, 50000.0, {"score": 0.9},
            "binance", "1h", f"{ts}-b", force_paper=True,
            stop_loss_price=49000.0, take_profit_price=52000.0,
        )
        assert result["status"] == "REJECTED"
        assert "normalized volume/volatility/spread context" in result["error"]
        async with SessionLocal() as db:
            trade = (await db.execute(select(Trade).where(Trade.id == result["trade_id"]))).scalar_one()
            assert trade.status == "REJECTED"
            assert trade.filled_quantity == 0.0
            assert trade.remaining_quantity == 0.0
            positions = (await db.execute(select(Position).where(Position.symbol == sym, Position.exchange == "binance"))).scalars().all()
            assert positions == []

    asyncio.run(run())
