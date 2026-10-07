from __future__ import annotations

from datetime import datetime, timezone
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import (
    Plan,
    Subscription,
    SubscriptionTradeUsage,
    SubscriptionTradeEvent,
)


ACTIVE_STATUSES = {"trialing", "active", "past_due"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def consume_subscription_trade(db, *, customer_id: int, idempotency_key: str) -> dict:
    """Atomically consume one monthly trade entitlement.

    The trade event is idempotent. A rejected/risk-blocked order never reaches this
    function, so only an execution that is about to create a Trade consumes usage.
    A monthly_trade_limit of 0 means no configured cap for that plan.
    """
    existing_event = (
        await db.execute(
            select(SubscriptionTradeEvent)
            .where(SubscriptionTradeEvent.idempotency_key == idempotency_key)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing_event:
        return {"consumed": False, "duplicate": True, "remaining": existing_event.remaining_after}

    sub = (
        await db.execute(
            select(Subscription)
            .where(
                Subscription.customer_id == customer_id,
                Subscription.status.in_(ACTIVE_STATUSES),
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not sub:
        raise ValueError("No active subscription")

    plan = (
        await db.execute(select(Plan).where(Plan.code == sub.plan_code, Plan.active.is_(True)))
    ).scalar_one_or_none()
    if not plan:
        raise ValueError("Subscription plan is unavailable")

    period_start = sub.current_period_start
    period_end = sub.current_period_end
    now = utcnow()
    if period_start is None or period_end is None or period_end <= now:
        raise ValueError("Subscription billing period has expired")

    usage = (
        await db.execute(
            select(SubscriptionTradeUsage)
            .where(
                SubscriptionTradeUsage.subscription_id == sub.id,
                SubscriptionTradeUsage.period_start == period_start,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if not usage:
        try:
            async with db.begin_nested():
                db.add(
                    SubscriptionTradeUsage(
                        subscription_id=sub.id,
                        customer_id=customer_id,
                        period_start=period_start,
                        period_end=period_end,
                        trades_used=0,
                    )
                )
                await db.flush()
        except IntegrityError:
            usage = (
                await db.execute(
                    select(SubscriptionTradeUsage)
                    .where(
                        SubscriptionTradeUsage.subscription_id == sub.id,
                        SubscriptionTradeUsage.period_start == period_start,
                    )
                    .with_for_update()
                )
            ).scalar_one()
        else:
            usage = (
                await db.execute(
                    select(SubscriptionTradeUsage)
                    .where(
                        SubscriptionTradeUsage.subscription_id == sub.id,
                        SubscriptionTradeUsage.period_start == period_start,
                    )
                    .with_for_update()
                )
            ).scalar_one()

    limit = int(plan.monthly_trade_limit or 0)
    used = int(usage.trades_used or 0)
    if limit > 0 and used >= limit:
        raise ValueError("Monthly trade entitlement has been reached")

    usage.trades_used = used + 1
    remaining = max(0, limit - usage.trades_used) if limit > 0 else None
    event = SubscriptionTradeEvent(
        subscription_id=sub.id,
        customer_id=customer_id,
        usage_id=usage.id,
        idempotency_key=idempotency_key,
        event_type="TRADE_CONSUMED",
        units=1,
        remaining_after=remaining,
        metadata_json=json.dumps({"plan": plan.code}, separators=(",", ":")),
    )
    db.add(event)
    await db.flush()
    return {"consumed": True, "duplicate": False, "remaining": remaining, "usage_id": usage.id}
