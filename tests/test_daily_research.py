import asyncio
import pandas as pd


def test_yahoo_normalization():
    from app.market_intelligence import _normalize_ohlcv
    idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    df = pd.DataFrame({"Open": [1,2,3], "High": [2,3,4], "Low": [0.5,1.5,2.5], "Close": [1.5,2.5,3.5], "Volume": [10,20,30]}, index=idx)
    out = _normalize_ohlcv(df)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.iloc[-1]["close"] == 3.5


def test_news_summary():
    from app.market_intelligence import summarize_news
    out = summarize_news([
        {"overall_sentiment_score": 0.2, "topics": [{"topic": "earnings"}]},
        {"overall_sentiment_score": -0.1, "topics": [{"topic": "earnings"}, {"topic": "economy"}]},
    ])
    assert out["article_count"] == 2
    assert abs(out["average_sentiment"] - 0.05) < 1e-9
    assert out["top_topics"][0] == ("earnings", 2)


def test_daily_run_does_not_trade(monkeypatch):
    import app.daily_research as dr
    class FakeDF:
        pass
    async def fake_lock(*args, **kwargs): return True
    async def fake_release(*args, **kwargs): return None
    async def fake_intel(*args, **kwargs):
        return {"histories": {}, "failures": {}, "news": [], "news_summary": {}, "news_error": ""}
    monkeypatch.setattr(dr, "acquire_lock", fake_lock)
    monkeypatch.setattr(dr, "release_lock", fake_release)
    monkeypatch.setattr(dr, "gather_market_intelligence", fake_intel)
    out = asyncio.run(dr.run_daily_research())
    assert out["status"] == "NO_DATA"
    assert out["real_money_execution"] is False


def test_daily_run_records_prospective_trial_archive(monkeypatch):
    import app.daily_research as dr

    class FakeFrame:
        pass

    async def fake_lock(*args, **kwargs):
        return True

    async def fake_release(*args, **kwargs):
        return None

    async def fake_intel(*args, **kwargs):
        return {
            "histories": {"BTC-USD": FakeFrame()},
            "failures": {},
            "news": [],
            "news_summary": {},
            "news_error": "",
        }

    async def fake_prior(_symbol):
        return set()

    def fake_backtest(*args, **kwargs):
        return {
            "asset_class": "crypto",
            "data_provenance": {},
            "validation": {"trial_labels": ["trend@fixture"]},
            "strategies": [],
            "universe_bias": "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY",
        }

    async def fake_archive(report, request):
        assert report["validation"]["trial_labels"] == ["trend@fixture"]
        assert request.symbol == "BTC-USD"
        return {
            "status": "APPENDED",
            "inserted": 1,
            "unverified_records": 1,
            "prospective_archive_only": True,
            "promotion_evidence_complete": False,
        }

    async def fake_persist(*args, **kwargs):
        return {"status": "FIRST_RUN", "flags": []}

    monkeypatch.setattr(dr, "acquire_lock", fake_lock)
    monkeypatch.setattr(dr, "release_lock", fake_release)
    monkeypatch.setattr(dr, "gather_market_intelligence", fake_intel)
    monkeypatch.setattr(dr, "_prior_trial_labels", fake_prior)
    monkeypatch.setattr(dr, "backtest_all_strategies", fake_backtest)
    monkeypatch.setattr(dr, "archive_research_trials", fake_archive)
    monkeypatch.setattr(dr, "_ai_research_brief", lambda *_: {"ai_brain_mode": "RESEARCH_ONLY"})
    monkeypatch.setattr(dr, "_persist_research_run", fake_persist)

    out = asyncio.run(dr.run_daily_research())
    assert out["status"] == "COMPLETE"
    assert out["real_money_execution"] is False
    assert out["runs"][0]["trial_archive"]["prospective_archive_only"] is True
    assert out["runs"][0]["research"]["prospective_trial_archive"]["promotion_evidence_complete"] is False


def test_async_retry_succeeds_after_transient_failure(monkeypatch):
    import app.daily_research as dr

    calls = {"count": 0}

    async def operation():
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("temporary source outage")
        return {"ok": True}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(dr.asyncio, "sleep", no_wait)
    result = asyncio.run(dr._retry_async(operation, attempts=3, base_delay_seconds=0))
    assert result == {"ok": True}
    assert calls["count"] == 3


def test_async_retry_raises_after_bounded_attempts(monkeypatch):
    import app.daily_research as dr

    calls = {"count": 0}

    async def operation():
        calls["count"] += 1
        raise RuntimeError("source unavailable")

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(dr.asyncio, "sleep", no_wait)
    try:
        asyncio.run(dr._retry_async(operation, attempts=3, base_delay_seconds=0))
    except RuntimeError as exc:
        assert str(exc) == "source unavailable"
    else:
        raise AssertionError("retry helper must propagate the final failure")
    assert calls["count"] == 3


def test_async_retry_does_not_swallow_cancellation():
    import app.daily_research as dr

    async def operation():
        raise asyncio.CancelledError()

    try:
        asyncio.run(dr._retry_async(operation, attempts=3, base_delay_seconds=0))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancellation must propagate immediately")


def test_daily_research_cancels_work_when_lock_lease_is_lost():
    import app.daily_research as dr

    cancelled = []

    async def failed_refresh(_key, *, ttl_seconds):
        return False

    async def slow_work():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    try:
        dr.refresh_lock = failed_refresh
        try:
            asyncio.run(
                dr._run_with_research_lease(
                    slow_work, "atlas:daily-research:test", 300,
                    refresh_interval_seconds=0.001,
                )
            )
        except dr._ResearchLeaseLost:
            pass
        else:
            raise AssertionError("lost lock lease must abort daily research")
    finally:
        from app.distributed import refresh_lock
        dr.refresh_lock = refresh_lock

    assert cancelled == [True]


def test_refresh_lock_logs_redis_renewal_exception(monkeypatch, caplog):
    import logging
    import app.distributed as distributed

    class BrokenRedis:
        async def eval(self, *_args):
            raise TimeoutError("redis renewal timed out")

    monkeypatch.setattr(distributed.settings, "redis_url", "redis://test.invalid")
    monkeypatch.setattr(distributed, "_client", BrokenRedis())
    token = distributed._lock_tokens.set({"daily-research-log-test": "owner-token"})
    try:
        with caplog.at_level(logging.WARNING, logger=distributed.__name__):
            assert asyncio.run(distributed.refresh_lock("daily-research-log-test", ttl_seconds=300)) is False
        assert "redis_lock_refresh_failed" in caplog.text
        assert "TimeoutError" in caplog.text
        assert "redis renewal timed out" in caplog.text
    finally:
        distributed._lock_tokens.reset(token)



def test_review_update_skips_when_research_run_insert_did_not_succeed(monkeypatch):
    import app.daily_research as dr

    def unexpected_session():
        raise AssertionError("must not query or update any pre-existing audit without the inserted row ID")

    monkeypatch.setattr(dr, "_update_persisted_research_review", dr._update_persisted_research_review)
    from app.db import ResearchRun  # noqa: F401 -- import guards model availability
    monkeypatch.setattr(dr, "settings", dr.settings)

    # The guard runs before SessionLocal is imported/accessed, so a missing inserted ID
    # cannot fall through and overwrite an earlier run for the same symbol.
    assert asyncio.run(dr._update_persisted_research_review(None, {"groq_research_review": {"status": "OK"}})) is False
