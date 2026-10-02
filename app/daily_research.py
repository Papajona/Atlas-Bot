from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .distributed import acquire_lock, release_lock
from .market_intelligence import gather_market_intelligence
from .research_engine import backtest_all_strategies, ai_market_review, paper_candidates
from .strategy_engine import StrategyConfig
from .ai_providers import dual_ai_research_review


def _cfg() -> StrategyConfig:
    return StrategyConfig(
        signal_threshold=settings.strategy_signal_threshold,
        target_vol_annual=settings.strategy_target_vol_annual,
        max_leverage=settings.strategy_max_leverage,
        stop_atr=settings.strategy_stop_atr,
        take_profit_atr=settings.strategy_take_profit_atr,
    )


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
    """Reduce a backtest_all_strategies() payload to the handful of fields tracked across runs."""
    regime = (research.get("regime") or {})
    order = research.get("research_order") or []
    top = order[0] if order else ""
    row = next((r for r in (research.get("strategies") or []) if r.get("strategy") == top), {}) or {}
    def f(key: str) -> float:
        try:
            v = float(row.get(key, 0.0))
            return v if v == v and abs(v) != float("inf") else 0.0
        except (TypeError, ValueError):
            return 0.0
    return {
        "regime": str(regime.get("regime", "")),
        "annualized_realized_vol": regime.get("annualized_realized_vol"),
        "top_strategy": str(top),
        "sharpe": f("sharpe"),
        "max_drawdown": f("max_drawdown"),
        "total_return": f("total_return"),
        "trial_labels": list((research.get("validation") or {}).get("trial_labels") or []),
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


async def _prior_trial_labels(symbol: str, days: int = 365) -> set[str]:
    """Union of every configuration label evaluated on this symbol in the last `days` (best-effort; empty set on failure)."""
    try:
        from datetime import timedelta
        from sqlalchemy import select
        from .db import SessionLocal, ResearchRun
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        async with SessionLocal() as db:
            rows = (await db.execute(select(ResearchRun.research_json).where(ResearchRun.symbol == symbol, ResearchRun.created_at >= cutoff))).scalars().all()
        labels: set[str] = set()
        for raw in rows:
            try:
                labels.update(str(x) for x in (json.loads(raw or "{}").get("trial_labels") or []))
            except (TypeError, ValueError):
                continue
        return labels
    except Exception:
        return set()


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
            db.add(ResearchRun(
                symbol=symbol[:80], regime_assessment=current["regime"][:60], top_strategy=current["top_strategy"][:40],
                sharpe=current["sharpe"], max_drawdown=current["max_drawdown"], total_return=current["total_return"],
                macro_risk_state=str(macro_regime.get("risk_state") or "")[:20],
                macro_risk_score=float((macro_regime.get("headline_risk") or {}).get("risk_score") or 0.0),
                regime_drift_json=json.dumps(drift, default=str),
                research_json=json.dumps(current, default=str),
                review_json=json.dumps({k: review.get(k) for k in ("ai_brain_mode", "paper_candidates")}, default=str),
            ))
            await db.commit()
        return drift
    except Exception as exc:
        return {"status": "PERSIST_FAILED", "error": str(exc), "flags": []}


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


async def run_daily_research() -> dict[str, Any]:
    lock_key = "atlas:daily-research:global"
    # Distinguish two very different reasons a lock can fail to be acquired, so callers
    # (and the audit trail) don't lump a benign "another instance already ran today" in
    # with "this deployment has no distributed-lock backend configured, so the daily
    # research/backtest job has never actually executed" -- the latter is a silent,
    # indefinite no-op in production without REDIS_URL and needs to stand out, not blend
    # into the normal success event.
    if settings.environment.lower() == "production" and not settings.redis_url:
        return {"status": "SKIPPED_NO_DISTRIBUTED_LOCK_BACKEND", "run_at": datetime.now(timezone.utc).isoformat()}
    acquired = await acquire_lock(lock_key, ttl_seconds=max(300, settings.daily_research_lock_seconds))
    if not acquired:
        return {"status": "ALREADY_RUNNING", "run_at": datetime.now(timezone.utc).isoformat()}
    try:
        symbols = [s.strip() for s in settings.research_symbols.split(",") if s.strip()]
        intelligence = await gather_market_intelligence(
            symbols=symbols,
            yahoo_period=settings.research_yahoo_period,
            alpha_vantage_api_key=settings.alpha_vantage_api_key,
        )
        runs = []
        cfg = _cfg()
        for symbol, df in intelligence["histories"].items():
            try:
                research = await asyncio.to_thread(
                    backtest_all_strategies,
                    df, cfg, settings.research_taker_bps, settings.research_slippage_bps,
                    True, settings.default_asset, settings.research_folds, settings.research_min_train,
                    settings.research_ai_threshold,
                    await _prior_trial_labels(symbol),
                )
                review = _ai_research_brief(research, intelligence)
                if settings.gemini_api_key or settings.groq_api_key:
                    review["external_ai"] = await dual_ai_research_review(research, intelligence)
                else:
                    review["external_ai"] = {
                        "status": "NOT_CONFIGURED",
                        "providers": {},
                        "execution_authority": False,
                    }
                review["run_drift"] = await _persist_research_run(symbol, research, review, intelligence.get("macro_regime"))
                runs.append({"symbol": symbol, "research": research, "review": review})
            except Exception as exc:
                runs.append({"symbol": symbol, "status": "FAILED", "error": str(exc)})
        return {
            "status": "COMPLETE" if runs else "NO_DATA",
            "run_at": datetime.now(timezone.utc).isoformat(),
            "symbols_requested": symbols,
            "source_intelligence": {k: v for k, v in intelligence.items() if k != "histories"},
            "runs": runs,
            "real_money_execution": False,
        }
    finally:
        await release_lock(lock_key)
