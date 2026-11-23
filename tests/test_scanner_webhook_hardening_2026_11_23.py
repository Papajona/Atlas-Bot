"""Regression tests for bounded market-scanner fan-out and webhook payload limits."""
import asyncio
import pathlib
import threading
import time

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import market_scanner as ms
from app.request_limits import read_limited_body

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def test_normalize_symbols_trims_dedupes_and_caps_in_order():
    raw = " BTC/USDT , ,ETH/USDT,BTC/USDT," + ",".join(
        f"S{i}/USDT" for i in range(30)
    )
    out = ms.normalize_symbols(raw)
    assert out[:2] == ["BTC/USDT", "ETH/USDT"]
    assert len(out) == ms.MAX_SYMBOLS
    assert len(set(out)) == len(out)


def test_normalize_symbols_drops_oversized_empty_and_control_character_values():
    assert ms.normalize_symbols("") == []
    assert ms.normalize_symbols(" , ,") == []
    assert ms.normalize_symbols("X" * (ms.MAX_SYMBOL_LENGTH + 1) + ",ETH/USDT") == [
        "ETH/USDT"
    ]
    assert ms.normalize_symbols("BTC/USDT\\r\\nFORGED,ETH/USDT") == ["ETH/USDT"]
    assert ms.normalize_symbols("BTC/USDT\\u2028FORGED,ETH/USDT") == ["ETH/USDT"]


def test_scan_symbols_never_exceeds_concurrency_bound():
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def fetch(symbol):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return symbol

    async def run():
        semaphore = asyncio.Semaphore(2)
        return await ms.scan_symbols(
            [f"S{i}" for i in range(8)],
            fetch=fetch,
            metrics=lambda frame: {"score": 0.5},
            semaphore=semaphore,
        )

    results = asyncio.run(run())
    assert state["peak"] <= 2
    assert [row["symbol"] for row in results] == [f"S{i}" for i in range(8)]
    assert all(row["score"] == 0.5 for row in results)


def test_scan_symbols_redacts_exception_text_and_logs_server_side():
    class Log:
        def __init__(self):
            self.calls = []

        def warning(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    logger = Log()

    def fetch(_symbol):
        raise RuntimeError("secret upstream detail https://api.example/key=abc123")

    async def run():
        return await ms.scan_symbols(
            ["BTC/USDT"],
            fetch=fetch,
            metrics=lambda _frame: {},
            semaphore=asyncio.Semaphore(1),
            logger=logger,
        )

    (row,) = asyncio.run(run())
    assert row == {
        "symbol": "BTC/USDT",
        "signal": "ERROR",
        "error": ms.GENERIC_ERROR,
    }
    assert "abc123" not in repr(row)
    assert len(logger.calls) == 1
    assert logger.calls[0][1].get("exc_info") is True


def test_scan_symbols_json_escapes_untrusted_symbol_in_logs():
    class Log:
        def __init__(self):
            self.calls = []

        def warning(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    logger = Log()

    async def run():
        return await ms.scan_symbols(
            ["BTC/USDT\\nFORGED"],
            fetch=lambda _symbol: (_ for _ in ()).throw(RuntimeError("upstream failed")),
            metrics=lambda _frame: {},
            semaphore=asyncio.Semaphore(1),
            logger=logger,
        )

    asyncio.run(run())
    logged_symbol = logger.calls[0][0][1]
    assert "\n" not in logged_symbol
    assert "\\nFORGED" in logged_symbol


def test_scan_symbols_metrics_failure_is_contained_and_semaphore_released():
    semaphore = asyncio.Semaphore(1)

    def bad_metrics(_frame):
        raise ValueError("boom")

    async def run():
        first = await ms.scan_symbols(
            ["A"],
            fetch=lambda symbol: symbol,
            metrics=bad_metrics,
            semaphore=semaphore,
        )
        second = await ms.scan_symbols(
            ["B"],
            fetch=lambda symbol: symbol,
            metrics=lambda _frame: {"score": 1.0},
            semaphore=semaphore,
        )
        return first, second

    first, second = asyncio.run(run())
    assert first[0]["signal"] == "ERROR"
    assert second[0]["score"] == 1.0


def test_main_wires_scanner_through_bounded_helper_and_validates_exchange():
    start = MAIN.index('@app.get("/api/customer/market-scanner")')
    following = MAIN.find('@app.get("/api/customer/market-intelligence")', start)
    block = MAIN[start:following if following >= 0 else start + 2500]
    assert "_scanner_scan_symbols(" in block
    assert "semaphore=_SCANNER_SEMAPHORE" in block
    assert "if exchange not in ccxt.exchanges" in block
    assert "str(exc)" not in block
    assert "asyncio.gather(" not in block
    assert (
        "_SCANNER_SEMAPHORE = asyncio.Semaphore(_SCANNER_MAX_CONCURRENT_FETCHES)"
        in MAIN
    )


def test_customer_webhook_rejects_oversized_body_before_database_access():
    start = MAIN.index('@app.post("/api/webhooks/{endpoint_id}")')
    block = MAIN[start:start + 1200]
    assert "read_limited_body(request, _CUSTOMER_WEBHOOK_MAX_BODY_BYTES)" in block
    assert block.index("read_limited_body(") < block.index("SessionLocal()")
    assert "_CUSTOMER_WEBHOOK_MAX_BODY_BYTES = 64 * 1024" in MAIN
    assert 'path_prefix_limits={"/api/webhooks/": _CUSTOMER_WEBHOOK_MAX_BODY_BYTES}' in MAIN
    assert "raw=await request.body()" not in block


def test_webhook_retention_is_external_privileged_dry_run_by_default():
    script = (ROOT / "scripts" / "prune_webhook_events.sql").read_text(encoding="utf-8")
    provision = (ROOT / "scripts" / "provision_atlas_api_runtime.sql").read_text(
        encoding="utf-8"
    )
    assert "DELETE FROM public.webhook_events" in script
    assert "WHERE created_at < now()" in script
    assert r"\set apply 0" in script
    assert r"\if :apply" in script
    assert "atlas_api_runtime" in script
    assert "retention_days_valid" in script
    assert "batch_size_valid" in script
    assert "\\quit 1" in script
    assert "ORDER BY created_at, id" in script
    assert "DELETE" in provision
    assert "unexpected_delete_privileges" in provision


def test_webhook_retention_index_is_created_and_matches_prune_order():
    migration = (ROOT / "alembic" / "versions" / "0060_webhook_events_created_at_index.py").read_text(encoding="utf-8")
    db_source = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
    assert 'revision = "0060_webhook_events_created_at_index"' in migration
    assert 'down_revision = "0059_research_integrity_controls"' in migration
    assert 'op.create_index(' in migration
    assert '["created_at", "id"]' in migration
    assert 'Index("ix_webhook_events_created_at_id", "created_at", "id")' in db_source


def test_research_request_defaults_follow_deployment_settings(monkeypatch):
    from app.config import settings
    from app.schemas import ResearchMarketRequest

    monkeypatch.setattr(settings, "research_folds", 7)
    monkeypatch.setattr(settings, "research_min_train", 1200)

    request = ResearchMarketRequest()
    assert request.folds == 7
    assert request.min_train == 1200


def test_research_request_rejects_out_of_range_deployment_defaults(monkeypatch):
    from app.config import settings
    from app.schemas import ResearchMarketRequest

    monkeypatch.setattr(settings, "research_folds", 1)
    with pytest.raises(ValidationError, match="folds"):
        ResearchMarketRequest()

    monkeypatch.setattr(settings, "research_folds", 5)
    monkeypatch.setattr(settings, "research_min_train", 99)
    with pytest.raises(ValidationError, match="min_train"):
        ResearchMarketRequest()


class _FakeStreamRequest:
    def __init__(self, chunks, content_length=None):
        self.chunks = list(chunks)
        self.consumed = 0
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = content_length

    async def stream(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


def test_webhook_streaming_reader_stops_on_first_oversized_chunk():
    request = _FakeStreamRequest([b"1234", b"56", b"ignored"])

    with pytest.raises(HTTPException) as error:
        asyncio.run(read_limited_body(request, 5))

    assert getattr(error.value, "status_code", None) == 413
    assert request.consumed == 2


def test_webhook_streaming_reader_rejects_declared_oversize_without_reading():
    request = _FakeStreamRequest([b"ignored"], content_length="65")
    with pytest.raises(Exception) as error:
        asyncio.run(read_limited_body(request, 64))
    assert getattr(error.value, "status_code", None) == 413
    assert request.consumed == 0


def test_webhook_streaming_reader_accepts_exact_limit_and_chunked_body():
    request = _FakeStreamRequest([b"123", b"456"])
    assert asyncio.run(read_limited_body(request, 6)) == b"123456"
    assert request.consumed == 2
