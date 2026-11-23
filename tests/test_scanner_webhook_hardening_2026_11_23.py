"""Regression tests for bounded market-scanner fan-out and webhook payload limits."""
import asyncio
import pathlib
import threading
import time

from app import market_scanner as ms

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


def test_normalize_symbols_drops_oversized_and_empty_input():
    assert ms.normalize_symbols("") == []
    assert ms.normalize_symbols(" , ,") == []
    assert ms.normalize_symbols("X" * (ms.MAX_SYMBOL_LENGTH + 1) + ",ETH/USDT") == [
        "ETH/USDT"
    ]


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
    cap_pos = block.index("_CUSTOMER_WEBHOOK_MAX_BODY_BYTES")
    assert block.index("raw=await request.body()") < cap_pos < block.index("SessionLocal()")
    assert "HTTPException(413" in block
    assert "_CUSTOMER_WEBHOOK_MAX_BODY_BYTES = 64 * 1024" in MAIN


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
    assert "DELETE" in provision
    assert "unexpected_delete_privileges" in provision


def test_research_request_defaults_follow_deployment_settings(monkeypatch):
    from app.config import settings
    from app.schemas import ResearchMarketRequest

    monkeypatch.setattr(settings, "research_folds", 7)
    monkeypatch.setattr(settings, "research_min_train", 1200)

    request = ResearchMarketRequest()
    assert request.folds == 7
    assert request.min_train == 1200
