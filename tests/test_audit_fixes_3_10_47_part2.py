"""Regression coverage for 3.10.47 risk-control audit fixes."""
import asyncio
import json
import types
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.execution as execution
import app.incidents as incidents
from app.config import Settings, settings
from app.db import AppState, Base, Incident

ROOT = Path(__file__).resolve().parents[1]

def _db(tmp_path, monkeypatch):
    # Incident.detail_json is an encrypted field. Use a deterministic test-only
    # Fernet key so these tests exercise the real encryption path without secrets.
    monkeypatch.setattr(settings, 'app_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=', raising=False)
    monkeypatch.setattr(settings, 'app_encryption_keys_json', '', raising=False)
    monkeypatch.setattr(settings, 'app_encryption_active_key_id', 'v1', raising=False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'risk.db'}")
    async def init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(init())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(execution, 'SessionLocal', maker)
    monkeypatch.setattr(incidents, 'SessionLocal', maker)
    return engine, maker

async def _seed(maker, **kw):
    values = dict(cash_equity=1000.0, equity=1000.0, peak_equity=1000.0, daily_start_equity=1000.0, live_enabled=True, mode='LIVE', kill_switch=False)
    values.update(kw)
    async with maker() as db:
        db.add(AppState(**values))
        await db.commit()

def test_h2_loss_limit_uses_synced_app_state_and_calls_stop(tmp_path, monkeypatch):
    engine, maker = _db(tmp_path, monkeypatch)
    calls = []
    async def stop(exchange=None):
        calls.append(exchange)
        return {'broker_halt_confirmed': True}
    async def audit(*args, **kwargs):
        return None
    monkeypatch.setattr(execution, 'emergency_stop', stop)
    monkeypatch.setattr(execution, 'audit', audit)
    async def run():
        await _seed(maker, equity=960.0, peak_equity=1000.0, daily_start_equity=1000.0)
        out = await execution.enforce_platform_loss_limits()
        await engine.dispose()
        return out
    out = asyncio.run(run())
    assert out['status'] == 'KILLED'
    assert len(calls) == 1

def test_h1_spot_residual_exposure_fails_broker_confirmation(tmp_path, monkeypatch):
    engine, maker = _db(tmp_path, monkeypatch)
    class FakeSpotBroker:
        config = types.SimpleNamespace(market_type='spot')
        def cancel_all_orders(self): return []
        def fetch_open_orders(self): return []
        def fetch_positions(self): return [{'symbol': 'BTC/USDT', 'contracts': 0.5}]
    monkeypatch.setattr(execution.Broker, 'get', classmethod(lambda cls, cfg: FakeSpotBroker()))
    monkeypatch.setattr(settings, 'exchange_api_key', 'FAKE', raising=False)
    monkeypatch.setattr(settings, 'exchange_api_secret', 'FAKE', raising=False)
    monkeypatch.setattr(settings, 'default_market_type', 'spot', raising=False)
    async def barrier(_timeout): return True
    async def audit(*args, **kwargs): return None
    monkeypatch.setattr(execution, '_wait_for_live_submission_barrier', barrier)
    monkeypatch.setattr(execution, 'audit', audit)
    async def run():
        await _seed(maker)
        out = await execution.emergency_stop('binance')
        await engine.dispose()
        return out
    out = asyncio.run(run())
    assert out['broker_halt_confirmed'] is False
    assert out['ok'] is False
    assert any('Unflattened platform spot positions remain' in f.get('error', '') for f in out['failures'])

def test_h1_barrier_failure_also_fails_confirmation(tmp_path, monkeypatch):
    engine, maker = _db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, 'exchange_api_key', '', raising=False)
    monkeypatch.setattr(settings, 'exchange_api_secret', '', raising=False)
    async def barrier(_timeout): return False
    async def audit(*args, **kwargs): return None
    monkeypatch.setattr(execution, '_wait_for_live_submission_barrier', barrier)
    monkeypatch.setattr(execution, 'audit', audit)
    async def run():
        await _seed(maker)
        out = await execution.emergency_stop(None)
        await engine.dispose()
        return out
    out = asyncio.run(run())
    assert out['broker_halt_confirmed'] is False
    assert out['ok'] is False

def test_alert_webhook_never_sends_incident_detail(tmp_path, monkeypatch):
    engine, maker = _db(tmp_path, monkeypatch)
    sent = []
    class Resp: status_code = 200
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, json=None): sent.append((url, json)); return Resp()
    import httpx
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    monkeypatch.setattr(settings, 'alert_webhook_url', 'https://hooks.example.invalid/x', raising=False)
    async def run():
        await incidents.open_incident(key='A1', severity='HIGH', category='TEST', summary='safe summary', detail={'secret':'do-not-send'})
        await incidents.open_incident(key='A1', severity='HIGH', category='TEST', summary='safe summary', detail={'secret':'do-not-send'})
        await incidents.open_incident(key='A1', severity='CRITICAL', category='TEST', summary='escalated', detail={'secret':'do-not-send'})
        await engine.dispose()
    asyncio.run(run())
    payload_text = json.dumps(sent)
    assert len(sent) == 2
    assert 'do-not-send' not in payload_text
    assert '[Atlas HIGH]' in sent[0][1]['text']
    assert '[Atlas CRITICAL]' in sent[1][1]['text']

def test_alert_webhook_requires_https():
    with pytest.raises(ValueError, match='ALERT_WEBHOOK_URL must be an https'):
        Settings(environment='test', alert_webhook_url='http://insecure.example')

def test_binance_stream_has_stability_gate():
    source = (ROOT / 'app' / 'binance_user_stream.py').read_text()
    assert 'stable_seconds: float = 30.0' in source
    assert 'connected_at = time.monotonic()' in source
    assert 'time.monotonic() - connected_at >= self.stable_seconds' in source