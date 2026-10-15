"""Focused regressions for exchange outage behavior from the attached audit."""
import asyncio
import random
import threading
import time

import ccxt
import pytest

from app.broker import Broker, BrokerConfig


class FakeClient:
    def __init__(self, **behavior):
        self.behavior = behavior
        self.has = {}

    def create_order(self, *args, **kwargs):
        action = self.behavior.get("create_order")
        if callable(action):
            return action()
        if isinstance(action, Exception):
            raise action
        return action

    def fetch_ticker(self, *args):
        action = self.behavior.get("fetch_ticker")
        if isinstance(action, Exception):
            raise action
        return action

    def feature_value(self, *args):
        action = self.behavior.get("feature_value")
        if isinstance(action, Exception):
            raise action
        return action


def test_network_error_during_feature_probe_is_not_reported_as_unsupported():
    broker = Broker(BrokerConfig("binance", "key", "secret"))
    broker.client = FakeClient(feature_value=ccxt.NetworkError("connection reset"))

    with pytest.raises(ccxt.NetworkError):
        broker.supports_feature("BTC/USDT", "stopLoss")


def test_hung_order_does_not_block_market_data_read():
    broker = Broker(BrokerConfig("binance", "key", "secret"))
    broker.client = FakeClient(
        create_order=lambda: time.sleep(1.0) or {"id": "order-1"},
        fetch_ticker={"last": 1.0},
    )
    worker = threading.Thread(
        target=broker.market_order,
        args=("BTC/USDT", "buy", 1.0, "cid-1"),
        daemon=True,
    )
    worker.start()
    time.sleep(0.05)

    started = time.monotonic()
    ticker = broker.ticker("BTC/USDT")
    elapsed = time.monotonic() - started
    worker.join(timeout=2.0)

    assert ticker["last"] == 1.0
    assert elapsed < 0.5


def test_failed_market_metadata_load_is_cooled_down(monkeypatch):
    calls = {"count": 0}

    class Exchange:
        def __init__(self, params):
            pass

        def load_markets(self):
            calls["count"] += 1
            raise ccxt.ExchangeNotAvailable("503")

    monkeypatch.setattr(ccxt, "binance", Exchange)
    broker = Broker(BrokerConfig("binance", "key", "secret", sandbox=False))

    for _ in range(50):
        with pytest.raises(ccxt.ExchangeNotAvailable):
            broker.ticker("BTC/USDT")

    assert calls["count"] <= 1


def test_user_stream_reconnect_uses_jitter(monkeypatch):
    import websockets
    from app.binance_user_stream import BinanceUserStreamSession

    sleeps = []
    stop = asyncio.Event()

    def fail_connect(*args, **kwargs):
        raise OSError("exchange unreachable")

    monkeypatch.setattr(websockets, "connect", fail_connect)
    monkeypatch.setattr(random, "uniform", lambda low, high: high * 0.75)
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 8:
            stop.set()
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def noop(*args):
        return None

    session = BinanceUserStreamSession("wss://example.invalid", lambda: {}, noop, noop)
    asyncio.run(session.run(stop))

    assert len(sleeps) >= 8
    assert len(set(sleeps[:5])) == 5


def test_position_query_unsupported_is_not_reported_as_flat():
    broker = Broker(BrokerConfig("binance", "key", "secret"))
    broker.client = FakeClient()
    with pytest.raises(RuntimeError, match="does not support authoritative open-position queries"):
        broker.fetch_positions()
