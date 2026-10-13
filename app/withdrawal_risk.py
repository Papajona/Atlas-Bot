from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import func, select, case

from .config import settings
from .db import Withdrawal, WithdrawalDestination
from .withdrawal_security import destination_fingerprint


def _decimal(value) -> Decimal:
    """Convert database/config/input numerics without introducing binary-float arithmetic."""
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


async def score_withdrawal(
    db,
    *,
    customer_id: int,
    wallet_balance: Decimal | str | int | float,
    amount: Decimal | str | int | float,
    destination: str,
    currency: str,
    network: str,
) -> dict:
    now = datetime.now(timezone.utc)
    since24 = now - timedelta(hours=24)
    since7 = now - timedelta(days=7)
    asset = str(currency or "").strip().upper()
    chain = str(network or "").strip().upper()
    amount_d = _decimal(amount)
    balance_d = _decimal(wallet_balance)

    # Velocity must be measured in one asset/network unit. Mixing (for example)
    # BTC and USDT makes the configured amount limits meaningless.
    scope = (
        Withdrawal.customer_id == customer_id,
        func.upper(Withdrawal.currency) == asset,
        func.upper(Withdrawal.network) == chain,
        Withdrawal.created_at >= since7,
        Withdrawal.status != "REJECTED",
    )
    totals = await db.execute(
        select(
            func.coalesce(
                func.sum(case((Withdrawal.created_at >= since24, Withdrawal.amount), else_=0)),
                0,
            ),
            func.coalesce(func.sum(Withdrawal.amount), 0),
            func.coalesce(
                func.sum(
                    case(
                        (
                            Withdrawal.created_at >= since24,
                            case((Withdrawal.status == "UNKNOWN", 1), else_=0),
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        ).where(*scope)
    )
    total24_raw, total7_raw, unknown24_raw = totals.one()
    total24 = _decimal(total24_raw)
    total7 = _decimal(total7_raw)
    unknown24 = int(unknown24_raw or 0)

    # The existing destination registry stores a currency/network-bound fingerprint,
    # avoiding decrypting every historical withdrawal just to determine familiarity.
    fingerprint = destination_fingerprint(destination, asset, chain)
    known_destination = (
        await db.execute(
            select(WithdrawalDestination.id)
            .where(
                WithdrawalDestination.customer_id == customer_id,
                WithdrawalDestination.currency == asset,
                WithdrawalDestination.network == chain,
                WithdrawalDestination.fingerprint == fingerprint,
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None

    score = 0.0
    flags: list[str] = []
    ratio = amount_d / balance_d if balance_d > 0 else Decimal("1")
    if not known_destination:
        score += 0.30
        flags.append("NEW_DESTINATION")

    ratio_block = _decimal(settings.withdrawal_balance_ratio_block)
    ratio_review = _decimal(settings.withdrawal_balance_ratio_review)
    if ratio >= ratio_block:
        score += 0.45
        flags.append("HIGH_BALANCE_RATIO")
    elif ratio >= ratio_review:
        score += 0.25
        flags.append("ELEVATED_BALANCE_RATIO")

    velocity24_limit = _decimal(settings.withdrawal_velocity_24h)
    velocity7_limit = _decimal(settings.withdrawal_velocity_7d)
    if total24 + amount_d > velocity24_limit:
        score += 0.35
        flags.append("24H_VELOCITY_LIMIT")
    elif total24 + amount_d > velocity24_limit * Decimal("0.75"):
        score += 0.15
        flags.append("24H_VELOCITY_ELEVATED")
    if total7 + amount_d > velocity7_limit:
        score += 0.25
        flags.append("7D_VELOCITY_LIMIT")
    if unknown24 >= settings.withdrawal_max_unknown_24h:
        score += 0.20
        flags.append("RECENT_UNKNOWN_PAYOUTS")
    if amount_d >= _decimal(settings.withdrawal_max_amount) * Decimal("0.8"):
        score += 0.10
        flags.append("NEAR_MAX_AMOUNT")
    if asset != "USDT" or chain != "TRON":
        score += 0.20
        flags.append("UNEXPECTED_ASSET_NETWORK")

    score = min(1.0, round(score, 4))
    decision = (
        "BLOCK"
        if score >= settings.withdrawal_block_score
        else "REVIEW"
        if score >= settings.withdrawal_review_score
        else "ALLOW"
    )
    # Preserve exact velocity totals at the API boundary; risk decisions above never
    # round through float. The score itself is a dimensionless policy value.
    return {
        "score": score,
        "flags": flags,
        "decision": decision,
        "velocity_24h": format(total24, "f"),
        "velocity_7d": format(total7, "f"),
        "balance_ratio": float(round(ratio, 4)),
        "known_destination": known_destination,
    }
