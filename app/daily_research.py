from __future__ import annotations
import math
import re
import logging
import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from types import SimpleNamespace

from .config import settings
from .distributed import acquire_lock, refresh_lock, release_lock
from .market_intelligence import gather_market_intelligence
from .research_engine import backtest_all_strategies, ai_market_review, paper_candidates
from .strategy_engine import StrategyConfig
from .native_ai import native_research_review
from .groq_ai import groq_research_review
from .research_trial_archive import archive_research_trials


async def _retry_async(operation, *, attempts: int = 3, base_delay_seconds: float = 1.0):
    """Retry a transient async research operation with bounded exponential backoff.

    Only the supplied operation is retried; completed per-symbol research is never
    rerun by this helper, avoiding duplicate persistence side effects.
    """
    if attempts < 1 or base_delay_seconds < 0:
        raise ValueError("attempts must be >= 1 and base_delay_seconds must be >= 0")
    last_error = None
    for attempt in range(attempts):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(base_delay_seconds * (2 ** attempt))
    assert last_error is not None
    raise last_error


def _cfg() -> StrategyConfig:
    return StrategyConfig(
        signal_threshold=settings.strategy_signal_threshold,
        target_vol_annual=settings.strategy_target_vol_annual,
        max_leverage=settings.strategy_max_leverage,
        stop_atr=settings.strategy_stop_atr,
        take_profit_atr=settings.strategy_take_profit_atr,
    )




_GROQ_ACTIONABLE_PATTERNS = (
    re.compile(
        r"(?i:\b(?:buy|sell|go long|go short|enter|exit|open|close))\s+"
        r"(?:\d+(?:\.\d+)?\s+)?"
        r"(?:[A-Za-z0-9]{2,}(?:[/_][A-Za-z0-9]{2,}){1,2}|"
        r"[A-Z]{3,}[A-Z0-9]*|[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*)"
        r"(?:\s+(?i:now|immediately|at market|market price|at)\b)?"
    ),
    re.compile(r"(?i:\b(?:place|submit|execute)\s+(?:a\s+)?(?:(?:buy|sell)\s+)?order\b)"),
    re.compile(
        r"(?i:\b(?:entry|stop[- ]loss|take[- ]profit|limit|trigger)"
        r"(?:\s+price)?\s*(?:=|:|at)\s*[$€£]?\d+(?:[.,]\d+)?)"
    ),
    re.compile(r"(?i:\b(?:buy|sell)\s+(?:now|immediately|at market)\b)"),
)
_GROQ_NEGATED_PREFIX = re.compile(
    r"(?i)(?:\bdo\s+not|\bdon['’]?t|\bdont|\bnever|\bshould\s+not|\bmust\s+not|\bnot\s+to|\bavoid)"
    r"(?:\s+(?:recommend|instruct|suggest|tell)(?:\s+to)?)?\s*$"
)


def _contains_actionable_groq_text(text: str) -> bool:
    for pattern in _GROQ_ACTIONABLE_PATTERNS:
        for match in pattern.finditer(text):
            prefix = text[max(0, match.start() - 80):match.start()]
            if not _GROQ_NEGATED_PREFIX.search(prefix):
                return True
    return False


def _validate_groq_research_result(value: Any) -> dict[str, Any]:
    """Validate untrusted Groq adapter output again at the persistence boundary."""
    if not isinstance(value, dict):
        raise ValueError("Groq result must be an object")
    status = value.get("status")
    if status in {"DISABLED", "ERROR"}:
        result: dict[str, Any] = {
            "status": status,
            "provider": "groq",
            "execution_authority": False,
        }
        for field in ("reason", "error", "cause"):
            item = value.get(field)
            if isinstance(item, str) and item.strip():
                result[field] = item.strip()[:240]
        http_status = value.get("http_status")
        if isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599:
            result["http_status"] = http_status
        return result

    if status != "OK" or value.get("provider") != "groq" or value.get("execution_authority") is not False:
        raise ValueError("Groq result does not have the reviewed research-only envelope")
    model = value.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("Groq result model is invalid")

    review = value.get("review")
    if not isinstance(review, dict):
        raise ValueError("Groq result must contain a validated review object")
    safe_review: dict[str, Any] = {}
    for field in ("regime_assessment", "research_only_summary"):
        item = review.get(field)
        if not isinstance(item, str) or not item.strip() or len(item) > 4000:
            raise ValueError(f"Groq review field {field} must be non-empty bounded text")
        cleaned = item.strip()
        if _contains_actionable_groq_text(cleaned):
            raise ValueError(f"Groq review field {field} contains an actionable trading instruction")
        safe_review[field] = cleaned

    for field in ("key_observations", "data_quality_flags", "strategy_watchlist"):
        items = review.get(field)
        if (
            not isinstance(items, list)
            or len(items) > 20
            or any(not isinstance(item, str) or len(item) > 1000 for item in items)
        ):
            raise ValueError(f"Groq review field {field} must be a bounded array of text")
        cleaned_items = [item.strip() for item in items if item.strip()]
        if any(_contains_actionable_groq_text(item) for item in cleaned_items):
            raise ValueError(f"Groq review field {field} contains an actionable trading instruction")
        safe_review[field] = cleaned_items

    confidence = review.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise ValueError("Groq review confidence must be finite and between 0 and 1")
    safe_review["confidence"] = float(confidence)
    return {
        "status": "OK",
        "provider": "groq",
        "model": model.strip(),
        "review": safe_review,
        "execution_authority": False,
    }

def _ai_research_brief(research: dict[str, Any], intelligence: dict[str, Any]) -> dict[str, Any]:
    """Deterministic research brain; optional external LLM can consume this structured brief.

    The brief deliberately separates observations from execution decisions. No strategy
    is promoted to real-money trading by this job.
    """
    review = ai_market_review(research)
    review["external_intelligence"] = {
        "news_summary": intelligence.get("news_summary", {}),
        "source_failures": intelligence.get("failures", {}),
    }
    review["ai_brain_mode"] = "RESEARCH_ONLY"
    review["paper_candidates"] = paper_candidates(research)
    review["next_actions"] = [
        "Continue paper/shadow observation for promoted candidates.",
        "Review review.run_drift (regime, top-strategy, Sharpe and drawdown changes vs the previous stored run for this symbol).",
        "Investigate data-source failures before treating the run as complete.",
    ]
    return review


def summarize_research(research: dict[str, Any]) -> dict[str, Any]:
    """Summarize out-of-sample evidence; never relabel an in-sample winner as validated."""
    regime = research.get("regime") or {}
    validation = research.get("validation") or {}
    oos_by_strategy = validation.get("per_strategy_oos") or {}
    order = research.get("research_order") or []
    # Skip strategies that could not be evaluated on held-out bars. An OOS measurement
    # is still not promotion approval; that separate gate must pass independently.
    def valid_oos(name: str) -> bool:
        metrics = oos_by_strategy.get(name) or {}
        if metrics.get("status") != "OK":
            return False
        try:
            return math.isfinite(float(metrics.get("oos_total_return")))
        except (TypeError, ValueError, OverflowError):
            return False

    top = next((name for name in order if valid_oos(name)), "")
    oos = oos_by_strategy.get(top) or {}

    def finite_oos(key: str) -> float:
        try:
            value = float(oos.get(key, 0.0))
            return value if value == value and abs(value) != float("inf") else 0.0
        except (TypeError, ValueError):
            return 0.0

    return {
        "regime": str(regime.get("regime", "")),
        "annualized_realized_vol": regime.get("annualized_realized_vol"),
        "top_strategy": str(top),
        "top_strategy_basis": "OUT_OF_SAMPLE_MEASUREMENT_ONLY",
        "top_strategy_oos_status": str(oos.get("status") or "NO_VALID_OOS_MEASUREMENT"),
        "sharpe": finite_oos("sharpe"),
        "max_drawdown": finite_oos("max_drawdown"),
        "total_return": finite_oos("oos_total_return"),
        "universe_bias": research.get("universe_bias") or (research.get("data_quality") or {}).get("universe_bias", "UNKNOWN"),
        "trial_labels": list(validation.get("trial_labels") or []),
    }


def compute_run_drift(previous: dict[str, Any] | None, current: dict[str, Any], *,
                      sharpe_drop_warn: float = 0.5, drawdown_worsen_warn: float = 0.05) -> dict[str, Any]:
    """Compare this run's summary to the previous run for the same symbol.

    This is the comparison daily_research's own next_actions text has always asked for
    ("compare next run against this run for regime and performance drift") but which was
    impossible because nothing was persisted.
    """
    if not previous:
        return {"status": "FIRST_RUN", "flags": []}
    flags: list[str] = []
    if previous.get("regime") and current.get("regime") and previous["regime"] != current["regime"]:
        flags.append("REGIME_CHANGED")
    if previous.get("top_strategy") and current.get("top_strategy") and previous["top_strategy"] != current["top_strategy"]:
        flags.append("TOP_STRATEGY_CHANGED")
    d_sharpe = current.get("sharpe", 0.0) - previous.get("sharpe", 0.0)
    d_dd = current.get("max_drawdown", 0.0) - previous.get("max_drawdown", 0.0)  # more negative == worse
    d_ret = current.get("total_return", 0.0) - previous.get("total_return", 0.0)
    if d_sharpe <= -sharpe_drop_warn:
        flags.append("SHARPE_DEGRADED")
    if d_dd <= -drawdown_worsen_warn:
        flags.append("DRAWDOWN_WORSENED")
    return {
        "status": "DRIFT_FLAGGED" if flags else "STABLE",
        "flags": flags,
        "previous_regime": previous.get("regime"), "current_regime": current.get("regime"),
        "previous_top_strategy": previous.get("top_strategy"), "current_top_strategy": current.get("top_strategy"),
        "delta_sharpe": round(d_sharpe, 4), "delta_max_drawdown": round(d_dd, 4), "delta_total_return": round(d_ret, 4),
    }


async def _prior_trial_labels(symbol: str, days: int | None = None) -> set[str]:
    """Load prior labels; malformed history fails closed rather than undercounting trials."""
    from datetime import timedelta
    from sqlalchemy import select
    from .db import SessionLocal, ResearchRun
    query = select(ResearchRun.research_json).where(ResearchRun.symbol == symbol)
    # Selection bias is cumulative; default to all persisted attempts rather than
    # quietly dropping a trial after a rolling one-year window.
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.where(ResearchRun.created_at >= cutoff)
    async with SessionLocal() as db:
        rows = (await db.execute(query)).scalars().all()
    labels: set[str] = set()
    for raw in rows:
        try:
            payload = json.loads(raw or "{}")
            if not isinstance(payload, dict):
                raise ValueError("persisted research run must be a JSON object")
            raw_labels = payload.get("trial_labels")
            if raw_labels is not None and not isinstance(raw_labels, list):
                raise ValueError("persisted trial_labels must be a JSON array")
            labels.update(str(x) for x in (raw_labels or []))
        except (TypeError, ValueError, AttributeError) as exc:
            logging.getLogger(__name__).error(
                "research_trial_registry_corrupt symbol=%s error_type=%s",
                symbol, type(exc).__name__,
            )
            raise RuntimeError(
                "Persisted research trial history is malformed; refusing promotion-grade research."
            ) from exc
    return labels


async def _persist_research_run(symbol: str, research: dict[str, Any], review: dict[str, Any],
                                macro_regime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Store this run and return its drift vs the previous run. Never raises: tracking is best-effort."""
    try:
        from sqlalchemy import select, desc
        from .db import SessionLocal, ResearchRun
        current = summarize_research(research)
        macro_regime = macro_regime or {}
        async with SessionLocal() as db:
            prev_row = (await db.execute(
                select(ResearchRun).where(ResearchRun.symbol == symbol).order_by(desc(ResearchRun.created_at)).limit(1)
            )).scalar_one_or_none()
            previous = None
            if prev_row is not None:
                previous = {"regime": prev_row.regime_assessment, "top_strategy": prev_row.top_strategy,
                            "sharpe": prev_row.sharpe, "max_drawdown": prev_row.max_drawdown, "total_return": prev_row.total_return}
            drift = compute_run_drift(previous, current)
            row = ResearchRun(
                symbol=symbol[:80], regime_assessment=current["regime"][:60], top_strategy=current["top_strategy"][:40],
                sharpe=current["sharpe"], max_drawdown=current["max_drawdown"], total_return=current["total_return"],
                macro_risk_state=str(macro_regime.get("risk_state") or "")[:20],
                macro_risk_score=float((macro_regime.get("headline_risk") or {}).get("risk_score") or 0.0),
                regime_drift_json=json.dumps(drift, default=str),
                research_json=json.dumps(current, default=str),
                review_json=json.dumps({k: review.get(k) for k in ("ai_brain_mode", "paper_candidates", "native_ai_review", "groq_research_review")}, default=str),
            )
            db.add(row)
            await db.commit()
            research_run_id = row.id
        if research_run_id is None:
            return {"status": "PERSIST_FAILED", "error": "inserted research run has no database ID", "flags": []}
        return {**drift, "_research_run_id": research_run_id}
    except Exception as exc:
        return {"status": "PERSIST_FAILED", "error": str(exc), "flags": []}


async def _update_persisted_research_review(
    research_run_id: int | None, review: dict[str, Any]
) -> bool:
    """Attach the optional reviewer only to the exact ResearchRun inserted for this analysis."""
    if research_run_id is None:
        # _persist_research_run reports failures without an ID; never fall back to another row.
        return False
    try:
        from sqlalchemy import select
        from .db import SessionLocal, ResearchRun

        async with SessionLocal() as db:
            row = (await db.execute(
                select(ResearchRun).where(ResearchRun.id == research_run_id).limit(1)
            )).scalar_one_or_none()
            if row is None:
                return False
            row.review_json = json.dumps(
                {
                    key: review.get(key)
                    for key in (
                        "ai_brain_mode",
                        "paper_candidates",
                        "native_ai_review",
                        "groq_research_review",
                    )
                },
                default=str,
            )
            await db.commit()
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "research_review_audit_update_failed research_run_id=%s error_type=%s",
            research_run_id,
            type(exc).__name__,
            exc_info=True,
        )
        return False


async def latest_macro_context(max_age_hours: float = 48.0) -> dict[str, Any]:
    """Most recent macro/news risk snapshot from any daily research run, for live decision context.

    Deliberately global rather than matched to a specific trading symbol: ResearchRun.symbol
    holds Yahoo Finance tickers (e.g. "BTC-USD", "EURUSD=X"), which don't line up with the
    exchange/broker symbol formats used by live bots ("BTC/USDT", "EUR_USD", Deriv symbols).
    Building that mapping is future work; until it exists, treating this as global macro
    context (fed by Fed/ECB feeds + broad index/commodity trend, not asset-specific) is the
    honest thing this data can support. Advisory only -- callers decide what to do with it.
    """
    try:
        from sqlalchemy import select, desc
        from .db import SessionLocal, ResearchRun
        async with SessionLocal() as db:
            row = (await db.execute(select(ResearchRun).order_by(desc(ResearchRun.created_at)).limit(1))).scalar_one_or_none()
        if row is None:
            return {"status": "NO_DATA"}
        age_hours = max(0.0, (datetime.now(timezone.utc) - _aware(row.created_at)).total_seconds() / 3600.0)
        if age_hours > max_age_hours:
            return {"status": "STALE", "age_hours": round(age_hours, 1)}
        return {
            "status": "OK",
            "risk_state": row.macro_risk_state or "UNKNOWN",
            "risk_score": row.macro_risk_score,
            "as_of": row.created_at.isoformat() if row.created_at else None,
            "age_hours": round(age_hours, 1),
            "source": "daily_research_yahoo_and_news",
        }
    except Exception as exc:
        return {"status": "ERROR", "reason": str(exc)}


def _aware(dt) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class _ResearchLeaseLost(RuntimeError):
    """Raised when the daily-research task no longer owns its distributed lease."""


async def _refresh_research_lease(
    lock_key: str, ttl_seconds: int, *, interval_seconds: float | None = None
) -> None:
    interval = interval_seconds
    if interval is None:
        interval = max(5.0, min(float(ttl_seconds) / 3.0, 60.0))
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await refresh_lock(lock_key, ttl_seconds=ttl_seconds)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "daily_research_lock_refresh_failed error_type=%s", type(exc).__name__
            )
            renewed = False
        if not renewed:
            raise _ResearchLeaseLost("Daily research lock lease was lost; cancelling the run.")


async def _run_with_research_lease(
    operation, lock_key: str, ttl_seconds: int, *, refresh_interval_seconds: float | None = None
):
    """Cancel research immediately when the owner-token-checked Redis lease is lost."""
    work_task = asyncio.create_task(operation())
    heartbeat_task = asyncio.create_task(
        _refresh_research_lease(lock_key, ttl_seconds, interval_seconds=refresh_interval_seconds)
    )
    try:
        done, _ = await asyncio.wait((work_task, heartbeat_task), return_when=asyncio.FIRST_COMPLETED)
        if heartbeat_task in done:
            error = heartbeat_task.exception()
            work_task.cancel()
            await asyncio.gather(work_task, return_exceptions=True)
            if error is not None:
                raise error
            raise _ResearchLeaseLost("Daily research lock heartbeat stopped unexpectedly.")
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        return await work_task
    except BaseException:
        for task in (work_task, heartbeat_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(work_task, heartbeat_task, return_exceptions=True)
        raise


async def run_daily_research() -> dict[str, Any]:
    lock_key = "atlas:daily-research:global"
    if settings.environment.lower() == "production" and not settings.redis_url:
        return {"status": "SKIPPED_NO_DISTRIBUTED_LOCK_BACKEND", "run_at": datetime.now(timezone.utc).isoformat()}
    lease_ttl_seconds = max(300, int(settings.daily_research_lock_seconds))
    acquired = await acquire_lock(lock_key, ttl_seconds=lease_ttl_seconds)
    if not acquired:
        return {"status": "ALREADY_RUNNING", "run_at": datetime.now(timezone.utc).isoformat()}
    try:
        return await _run_with_research_lease(_run_daily_research_work, lock_key, lease_ttl_seconds)
    except _ResearchLeaseLost:
        return {
            "status": "FAILED", "stage": "distributed_lock", "error": "LOCK_LEASE_LOST",
            "run_at": datetime.now(timezone.utc).isoformat(), "real_money_execution": False,
        }
    finally:
        await release_lock(lock_key)


async def _run_daily_research_work() -> dict[str, Any]:
    symbols = [s.strip() for s in settings.research_symbols.split(",") if s.strip()]
    try:
        intelligence = await _retry_async(
            lambda: gather_market_intelligence(
                symbols=symbols,
                yahoo_period=settings.research_yahoo_period,
                alpha_vantage_api_key=settings.alpha_vantage_api_key,
            ),
            attempts=3,
            base_delay_seconds=1.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {
            "status": "FAILED",
            "stage": "market_intelligence",
            "attempts": 3,
            "error": f"{type(exc).__name__}: {exc}",
            "run_at": datetime.now(timezone.utc).isoformat(),
            "real_money_execution": False,
        }
    cfg = _cfg()
    run_records: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    groq_semaphore = asyncio.Semaphore(max(1, min(3, len(intelligence["histories"]))))

    async def bounded_groq_review(research_input: dict[str, Any]) -> dict[str, Any]:
        # Keep external advisory traffic bounded while allowing reviews for different
        # symbols to run alongside the remaining local backtests.
        async with groq_semaphore:
            return await groq_research_review(research_input, intelligence)

    try:
        for symbol, df in intelligence["histories"].items():
            try:
                research = await asyncio.to_thread(
                    backtest_all_strategies,
                    df, cfg, settings.research_taker_bps, settings.research_slippage_bps,
                    True, settings.default_asset, settings.research_folds, settings.research_min_train,
                    settings.research_ai_threshold,
                    await _prior_trial_labels(symbol),
                )
                trial_archive = await archive_research_trials(
                    research,
                    SimpleNamespace(symbol=symbol, asset=settings.default_asset),
                )
                # Prospective records are explicitly UNVERIFIED until complete
                # code/data/venue fingerprints exist; they cannot satisfy promotion.
                research["prospective_trial_archive"] = trial_archive
                review = _ai_research_brief(research, intelligence)
                # Local Native AI remains canonical and receives the computed candidates.
                native_research_input = dict(research)
                native_research_input["paper_candidates"] = review.get(
                    "paper_candidates", research.get("paper_candidates", [])
                )
                review["native_ai_review"] = native_research_review(native_research_input, intelligence)

                # Commit the local result and native review before any optional external
                # request can block or be cancelled. A lost lease must not erase completed
                # research evidence from the audit database.
                persistence_result = await _persist_research_run(
                    symbol, research, review, intelligence.get("macro_regime")
                )
                research_run_id = (
                    persistence_result.pop("_research_run_id", None)
                    if isinstance(persistence_result, dict)
                    else None
                )
                review["run_drift"] = persistence_result
                run_record = {
                    "symbol": symbol,
                    "research": research,
                    "review": review,
                    "trial_archive": trial_archive,
                }
                if trial_archive.get("status") == "FAILED":
                    run_record["status"] = "FAILED"
                    run_record["error"] = f"trial_archive:{trial_archive.get('reason', 'UNKNOWN')}"
                run_records[symbol] = run_record

                # Launch each optional Groq review as soon as its local analysis is ready.
                # At most three external requests run at once; failures stay advisory-only.
                groq_task = asyncio.create_task(
                    bounded_groq_review(native_research_input),
                    name=f"atlas-groq-research-review-{symbol}",
                )
                pending.append({
                    "symbol": symbol,
                    "research": research,
                    "review": review,
                    "trial_archive": trial_archive,
                    "research_run_id": research_run_id,
                    "groq_task": groq_task,
                })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                run_records[symbol] = {"symbol": symbol, "status": "FAILED", "error": str(exc)}

        # Await all advisory reviews together after local per-symbol work has progressed.
        # gather(return_exceptions=True) prevents a provider failure from hiding other results.
        results = await asyncio.gather(
            *(item["groq_task"] for item in pending),
            return_exceptions=True,
        )
        for item, groq_result in zip(pending, results):
            symbol = item["symbol"]
            review = item["review"]
            if isinstance(groq_result, Exception):
                review["groq_research_review"] = {
                    "status": "ERROR",
                    "provider": "groq",
                    "error": type(groq_result).__name__,
                    "execution_authority": False,
                }
            elif isinstance(groq_result, BaseException):
                raise groq_result
            elif isinstance(groq_result, dict):
                try:
                    # Validate even branch-specific adapters at the trusted consumer
                    # boundary. Never persist arbitrary provider output as commentary.
                    review["groq_research_review"] = _validate_groq_research_result(groq_result)
                except (TypeError, ValueError, OverflowError):
                    review["groq_research_review"] = {
                        "status": "ERROR",
                        "provider": "groq",
                        "error": "InvalidOrUnsafeReviewResult",
                        "execution_authority": False,
                    }
            else:
                review["groq_research_review"] = {
                    "status": "ERROR",
                    "provider": "groq",
                    "error": "InvalidReviewResult",
                    "execution_authority": False,
                }

            # Research was committed before this optional task was launched. Update its
            # review payload in place; never create a second run record for the same work.
            await _update_persisted_research_review(item.get("research_run_id"), review)
    except BaseException:
        # A lost lease cancels this operation. Do not leave detached provider tasks
        # running after the lease owner has stopped.
        tasks = [item["groq_task"] for item in pending]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        raise

    runs = [run_records[symbol] for symbol in intelligence["histories"] if symbol in run_records]
    return {
        "status": ("PARTIAL_FAILURE" if any(r.get("status") == "FAILED" for r in runs) else ("COMPLETE" if runs else "NO_DATA")),
        "run_at": datetime.now(timezone.utc).isoformat(),
        "symbols_requested": symbols,
        "source_intelligence": {k: v for k, v in intelligence.items() if k != "histories"},
        "runs": runs,
        "real_money_execution": False,
    }
