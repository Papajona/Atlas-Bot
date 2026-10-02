from __future__ import annotations
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_UP
import json
import logging
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from .db import CustomerLedgerAccount, Incident, LedgerEntry, LedgerJournal, LedgerJournalLine, Wallet, TradingAccount, utcnow

USDT = "USDT"
D = Decimal
logger = logging.getLogger(__name__)
_SCALE = Decimal("0.000001")  # matches the Numeric(38,6) ledger columns


def _q(value) -> Decimal:
    """Quantize to ledger precision (half-even) so journals and balances agree with the database."""
    return D(str(value)).quantize(_SCALE, rounding=ROUND_HALF_EVEN)


def _q_up(value) -> Decimal:
    """Quantize upward: reserves must never be short by a rounding remainder."""
    return D(str(value)).quantize(_SCALE, rounding=ROUND_UP)


async def record_ledger_incident(db, *, key: str, summary: str, detail: dict, customer_id: int | None, severity: str = "CRITICAL") -> None:
    """Write a CRITICAL incident in the caller's transaction (no second session, no lost update)."""
    row = (await db.execute(select(Incident).where(Incident.incident_key == key))).scalar_one_or_none()
    payload = json.dumps(detail, default=str)
    if row:
        row.severity, row.status, row.summary, row.detail_json, row.updated_at = severity, "OPEN", summary[:500], payload, utcnow()
        return
    db.add(Incident(incident_key=key, severity=severity, status="OPEN", category="LEDGER_RECONCILIATION",
                    customer_id=customer_id, summary=summary[:500], detail_json=payload))

async def get_or_create_ledger(db, customer_id: int, currency: str = USDT) -> CustomerLedgerAccount:
    row = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == currency,
    ).with_for_update())).scalar_one_or_none()
    if row:
        return row
    # Concurrent first-use requests can both observe no ledger row. Keep the
    # uniqueness race inside a savepoint, then reuse the committed winner.
    try:
        async with db.begin_nested():
            row = CustomerLedgerAccount(customer_id=customer_id, currency=currency)
            db.add(row)
            await db.flush()
    except IntegrityError:
        row = (await db.execute(select(CustomerLedgerAccount).where(
            CustomerLedgerAccount.customer_id == customer_id,
            CustomerLedgerAccount.currency == currency,
        ).with_for_update())).scalar_one_or_none()
        if not row:
            raise
        return row
    # One-time migration of legacy wallet balance into the authoritative ledger.
    wallet = (await db.execute(select(Wallet).where(Wallet.customer_id == customer_id, Wallet.currency == currency).with_for_update())).scalar_one_or_none()
    if wallet and float(wallet.available_balance or 0) > 0:
        row.available = D(str(wallet.available_balance))
        row.trading_reserved = 0
        row.withdrawal_reserved = 0
    return row

async def post_journal(db, *, currency: str, entry_type: str, reference_type: str, reference_id: str, idempotency_key: str, lines: list[dict], description: str = "") -> LedgerJournal:
    """Create an immutable balanced double-entry journal. All amounts are Decimal-safe."""
    existing = (await db.execute(select(LedgerJournal).where(
        LedgerJournal.idempotency_key == idempotency_key
    ).with_for_update())).scalar_one_or_none()
    if existing:
        return existing
    total_debit = sum((D(str(line.get("debit", 0))) for line in lines), D("0"))
    total_credit = sum((D(str(line.get("credit", 0))) for line in lines), D("0"))
    if total_debit != total_credit:
        raise ValueError("ledger journal is not balanced")
    if total_debit <= 0:
        raise ValueError("ledger journal must contain a positive amount")
    journal = LedgerJournal(currency=currency, entry_type=entry_type, reference_type=reference_type,
                            reference_id=reference_id, idempotency_key=idempotency_key, description=description)
    db.add(journal)
    await db.flush()
    for idx, line in enumerate(lines, start=1):
        debit = D(str(line.get("debit", 0)))
        credit = D(str(line.get("credit", 0)))
        if debit < 0 or credit < 0 or (debit > 0 and credit > 0):
            raise ValueError("journal line must contain either debit or credit")
        db.add(LedgerJournalLine(journal_id=journal.id, line_no=idx, account_code=str(line["account_code"]),
                                 customer_id=line.get("customer_id"), currency=currency, debit=debit, credit=credit))
    return journal


def _customer_account(customer_id: int, bucket: str, currency: str = USDT) -> str:
    return f"LIABILITY:CUSTOMER:{customer_id}:{currency}:{bucket}"


async def post_deposit(db, *, customer_id: int, wallet_id: int, amount: float, provider_reference: str, metadata: dict,
                       provider: str = "") -> CustomerLedgerAccount:
    amount_d = _q(amount)
    if amount_d <= 0:
        raise ValueError("deposit amount must be positive")
    ledger = await get_or_create_ledger(db, customer_id, USDT)
    legacy_idem = f"deposit:{provider_reference}"
    idem = f"deposit:{provider}:{provider_reference}" if provider else legacy_idem
    keys = {idem, legacy_idem}
    existing = (await db.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key.in_(keys)))).scalars().first()
    if existing:
        return ledger
    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()
    fresh_ledger.available = D(str(fresh_ledger.available)) + amount_d
    await post_journal(db, currency=USDT, entry_type="DEPOSIT_CREDIT", reference_type="TRON_TX",
                       reference_id=provider_reference, idempotency_key=idem,
                       lines=[
                           {"account_code": f"ASSET:TRON:DEPOSIT:{wallet_id}", "debit": amount_d, "credit": 0},
                           {"account_code": _customer_account(customer_id, "AVAILABLE"), "customer_id": customer_id, "debit": 0, "credit": amount_d},
                       ], description="Confirmed customer USDT deposit")
    db.add(LedgerEntry(customer_id=customer_id, currency=USDT, entry_type="DEPOSIT_CREDIT",
                       debit=0, credit=amount_d, amount=amount_d, reference_type="TRON_TX",
                       reference_id=provider_reference, idempotency_key=idem,
                       metadata_json=json.dumps({"wallet_id": wallet_id, **metadata}, separators=(",", ":"))))
    return fresh_ledger

async def sync_wallet_from_ledger(db, customer_id: int, currency: str = USDT) -> None:
    ledger = await get_or_create_ledger(db, customer_id, currency)
    wallet = (await db.execute(select(Wallet).where(Wallet.customer_id == customer_id, Wallet.currency == currency).with_for_update())).scalar_one_or_none()
    if wallet:
        wallet.available_balance = float(ledger.available)
        wallet.locked_balance = float(ledger.trading_reserved + ledger.withdrawal_reserved)
        wallet.updated_at = utcnow()

async def reserve_trading(db, customer_id: int, amount: float, *, reference_id: str) -> CustomerLedgerAccount:
    """Reserve customer cash for trading.

    ``reference_id`` must be stable across retries of the same logical reservation
    (e.g. derived from a trade id and fill count) so that a duplicate call -- caused
    by a client retry, a transaction retry, or duplicate event delivery -- is
    recognized by post_journal()'s idempotency check instead of reserving the
    customer's cash a second time.
    """
    amount_d = _q_up(amount)
    if amount_d <= 0:
        raise ValueError("reserve amount must be positive")
    reference_id = str(reference_id or "").strip()
    if not reference_id:
        raise ValueError("reserve_trading requires a stable reference_id")
    ledger = await get_or_create_ledger(db, customer_id, USDT)
    idem = f"reserve:{reference_id}"
    if (await db.execute(select(LedgerJournal).where(
        LedgerJournal.idempotency_key == idem
    ).with_for_update())).scalar_one_or_none():
        return ledger

    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()

    if D(str(fresh_ledger.available)) < amount_d:
        raise ValueError("insufficient available customer balance")
    fresh_ledger.available = D(str(fresh_ledger.available)) - amount_d
    fresh_ledger.trading_reserved = D(str(fresh_ledger.trading_reserved)) + amount_d
    await post_journal(db, currency=USDT, entry_type="TRADING_RESERVE", reference_type="TRADING",
                       reference_id=reference_id, idempotency_key=idem,
                       lines=[
                           {"account_code": _customer_account(customer_id, "AVAILABLE"), "customer_id": customer_id, "debit": amount_d, "credit": 0},
                           {"account_code": _customer_account(customer_id, "TRADING_RESERVED"), "customer_id": customer_id, "debit": 0, "credit": amount_d},
                       ], description="Reserve customer cash for trading")
    db.add(LedgerEntry(customer_id=customer_id, currency=USDT, entry_type="TRADING_RESERVE",
                       debit=amount_d, credit=0, amount=amount_d, reference_type="TRADING",
                       reference_id=reference_id, idempotency_key=idem))
    return fresh_ledger

async def release_trading(db, customer_id: int, amount: float, *, reference_id: str) -> CustomerLedgerAccount:
    amount_d = _q(amount)
    ledger = await get_or_create_ledger(db, customer_id, USDT)
    idem = f"release:{reference_id}:{customer_id}"
    if (await db.execute(select(LedgerJournal).where(LedgerJournal.idempotency_key == idem))).scalar_one_or_none():
        return ledger
    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()
    release = min(amount_d, fresh_ledger.trading_reserved)
    if release <= 0:
        return fresh_ledger
    fresh_ledger.trading_reserved -= release
    fresh_ledger.available += release
    await post_journal(db, currency=USDT, entry_type="TRADING_RELEASE", reference_type="TRADE",
                       reference_id=reference_id, idempotency_key=idem,
                       lines=[
                           {"account_code": _customer_account(customer_id, "TRADING_RESERVED"), "customer_id": customer_id, "debit": release, "credit": 0},
                           {"account_code": _customer_account(customer_id, "AVAILABLE"), "customer_id": customer_id, "debit": 0, "credit": release},
                       ], description="Release unused customer trading reserve")
    db.add(LedgerEntry(customer_id=customer_id, currency=USDT, entry_type="TRADING_RELEASE",
                       debit=0, credit=release, amount=release, reference_type="TRADE",
                       reference_id=reference_id, idempotency_key=idem))
    return fresh_ledger

async def reserve_withdrawal(db, customer_id: int, amount: float, *, reference_id: str) -> CustomerLedgerAccount:
    amount_d = _q(amount)
    if amount_d <= 0:
        raise ValueError("withdrawal reserve amount must be positive")
    ledger = await get_or_create_ledger(db, customer_id, USDT)
    idem = f"withdrawal-reserve:{reference_id}"
    if (await db.execute(select(LedgerJournal).where(
        LedgerJournal.idempotency_key == idem
    ).with_for_update())).scalar_one_or_none():
        return ledger

    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()

    if D(str(fresh_ledger.available)) < amount_d:
        raise ValueError("insufficient available customer balance")
    fresh_ledger.available = D(str(fresh_ledger.available)) - amount_d
    fresh_ledger.withdrawal_reserved = D(str(fresh_ledger.withdrawal_reserved)) + amount_d
    await post_journal(db, currency=USDT, entry_type="WITHDRAWAL_RESERVE", reference_type="WITHDRAWAL",
                       reference_id=reference_id, idempotency_key=idem,
                       lines=[
                           {"account_code": _customer_account(customer_id, "AVAILABLE"), "customer_id": customer_id, "debit": amount_d, "credit": 0},
                           {"account_code": _customer_account(customer_id, "WITHDRAWAL_RESERVED"), "customer_id": customer_id, "debit": 0, "credit": amount_d},
                       ], description="Reserve customer cash for withdrawal")
    db.add(LedgerEntry(customer_id=customer_id, currency=USDT, entry_type="WITHDRAWAL_RESERVE",
                       debit=amount_d, credit=0, amount=amount_d, reference_type="WITHDRAWAL",
                       reference_id=reference_id, idempotency_key=idem))
    return fresh_ledger

async def release_withdrawal(db, customer_id: int, amount: float, *, reference_id: str) -> CustomerLedgerAccount:
    amount_d = _q(amount)
    if amount_d <= 0:
        raise ValueError("withdrawal release amount must be positive")
    ledger = await get_or_create_ledger(db, customer_id, USDT)
    idem = f"withdrawal-release:{reference_id}"
    if (await db.execute(select(LedgerJournal).where(LedgerJournal.idempotency_key == idem))).scalar_one_or_none():
        return ledger
    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()
    release = min(amount_d, D(str(fresh_ledger.withdrawal_reserved)))
    if release <= 0:
        return fresh_ledger
    fresh_ledger.withdrawal_reserved = D(str(fresh_ledger.withdrawal_reserved)) - release
    fresh_ledger.available = D(str(fresh_ledger.available)) + release
    await post_journal(db, currency=USDT, entry_type="WITHDRAWAL_RELEASE", reference_type="WITHDRAWAL",
                       reference_id=reference_id, idempotency_key=idem,
                       lines=[
                           {"account_code": _customer_account(customer_id, "WITHDRAWAL_RESERVED"), "customer_id": customer_id, "debit": release, "credit": 0},
                           {"account_code": _customer_account(customer_id, "AVAILABLE"), "customer_id": customer_id, "debit": 0, "credit": release},
                       ], description="Release failed or rejected customer withdrawal reserve")
    db.add(LedgerEntry(customer_id=customer_id, currency=USDT, entry_type="WITHDRAWAL_RELEASE",
                       debit=0, credit=release, amount=release, reference_type="WITHDRAWAL",
                       reference_id=reference_id, idempotency_key=idem))
    return fresh_ledger

async def settle_withdrawal(db, customer_id: int, amount: float, *, reference_id: str) -> CustomerLedgerAccount:
    amount_d = _q(amount)
    if amount_d <= 0:
        raise ValueError("withdrawal settlement amount must be positive")
    ledger = await get_or_create_ledger(db, customer_id, USDT)
    idem = f"withdrawal-settlement:{reference_id}"
    if (await db.execute(select(LedgerJournal).where(LedgerJournal.idempotency_key == idem))).scalar_one_or_none():
        return ledger
    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()
    reserved_now = D(str(fresh_ledger.withdrawal_reserved))
    settle = min(amount_d, reserved_now)
    if reserved_now < amount_d:
        shortfall = amount_d - reserved_now
        logger.critical("withdrawal_reserve_shortfall customer=%s reference=%s payout=%s reserved=%s shortfall=%s",
                        customer_id, reference_id, amount_d, reserved_now, shortfall)
        await record_ledger_incident(
            db, key=f"WITHDRAWAL_RESERVE_SHORTFALL:{reference_id}",
            summary="Withdrawal payout exceeded the customer's withdrawal reserve",
            detail={"customer_id": customer_id, "reference_id": reference_id, "payout": str(amount_d),
                    "reserved": str(reserved_now), "shortfall": str(shortfall)}, customer_id=customer_id)
    if settle <= 0:
        return fresh_ledger
    fresh_ledger.withdrawal_reserved = D(str(fresh_ledger.withdrawal_reserved)) - settle
    await post_journal(db, currency=USDT, entry_type="WITHDRAWAL_SETTLEMENT", reference_type="WITHDRAWAL",
                       reference_id=reference_id, idempotency_key=idem,
                       lines=[
                           {"account_code": _customer_account(customer_id, "WITHDRAWAL_RESERVED"), "customer_id": customer_id, "debit": settle, "credit": 0},
                           {"account_code": "ASSET:TRON:WITHDRAWAL", "debit": 0, "credit": settle},
                       ], description="Settle completed customer withdrawal")
    db.add(LedgerEntry(customer_id=customer_id, currency=USDT, entry_type="WITHDRAWAL_SETTLEMENT",
                       debit=settle, credit=0, amount=settle, reference_type="WITHDRAWAL",
                       reference_id=reference_id, idempotency_key=idem))
    return fresh_ledger

async def customer_balance(db, customer_id: int, currency: str = USDT) -> dict:
    ledger = await get_or_create_ledger(db, customer_id, currency)
    return {"available": float(ledger.available), "trading_reserved": float(ledger.trading_reserved),
            "withdrawal_reserved": float(ledger.withdrawal_reserved),
            "total": float(D(str(ledger.available)) + D(str(ledger.trading_reserved)) + D(str(ledger.withdrawal_reserved)))}


DEFICIT_ACCOUNT = "ASSET:RECEIVABLE:CUSTOMER_DEFICIT"


async def settle_realized_pnl(db, *, customer_id: int, amount: float, reference_id: str, strict: bool = True) -> None:
    """Post realized USDT P&L into the customer liability ledger exactly once.

    ``strict=True`` (default) raises when a loss exceeds ``available`` -- right for pre-trade checks.
    Fill handling must call with ``strict=False``: the broker fill already happened, so bookkeeping
    must not be aborted. The loss is then absorbed up to ``available`` (the DB forbids negative
    balances) and the uncollected remainder is journaled to a customer-deficit receivable and
    raised as a CRITICAL incident, so the shortfall is explicit instead of lost.
    """
    pnl = _q(amount)
    if pnl == 0:
        return
    ledger = await get_or_create_ledger(db, customer_id, USDT)
    idem = f"settlement:{reference_id}"
    if (await db.execute(select(LedgerJournal).where(
        LedgerJournal.idempotency_key == idem
    ).with_for_update())).scalar_one_or_none():
        return

    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()

    available = D(str(fresh_ledger.available))
    shortfall = D("0")
    if pnl > 0:
        lines = [
            {"account_code": "ASSET:TRADING:SETTLEMENT", "debit": pnl, "credit": 0},
            {"account_code": _customer_account(customer_id, "AVAILABLE"), "customer_id": customer_id, "debit": 0, "credit": pnl},
        ]
        delta = pnl
    else:
        loss = abs(pnl)
        if available < loss and strict:
            raise ValueError("customer ledger cannot absorb realized trading loss")
        absorbed = min(available, loss)
        shortfall = loss - absorbed
        lines = []
        if absorbed > 0:
            lines.append({"account_code": _customer_account(customer_id, "AVAILABLE"), "customer_id": customer_id, "debit": absorbed, "credit": 0})
        if shortfall > 0:
            lines.append({"account_code": DEFICIT_ACCOUNT, "customer_id": customer_id, "debit": shortfall, "credit": 0})
        lines.append({"account_code": "ASSET:TRADING:SETTLEMENT", "debit": 0, "credit": loss})
        delta = -absorbed
    await post_journal(db, currency=USDT, entry_type="TRADE_SETTLEMENT", reference_type="TRADE",
                       reference_id=reference_id, idempotency_key=idem, lines=lines,
                       description="Realized trading P&L settlement")
    fresh_ledger.available = available + delta
    if shortfall > 0:
        logger.critical("realized_loss_exceeds_available customer=%s reference=%s loss=%s shortfall=%s",
                        customer_id, reference_id, abs(pnl), shortfall)
        await record_ledger_incident(
            db, key=f"CUSTOMER_DEFICIT:{reference_id}",
            summary="Realized trading loss exceeded the customer's available balance",
            detail={"customer_id": customer_id, "reference_id": reference_id, "loss": str(abs(pnl)),
                    "available_before": str(available), "shortfall": str(shortfall)}, customer_id=customer_id)


async def settle_trading_fee(db, *, customer_id: int, fee: float, reference_id: str, strict: bool = True) -> None:
    """Debit a trading fee from the customer's liability buckets exactly once.

    ``strict=False`` is for fill handling (see settle_realized_pnl): absorb what the customer
    has, journal the remainder to the customer-deficit receivable and open a CRITICAL incident.
    """
    fee_d = _q(fee)
    if fee_d <= 0:
        return
    ledger = await get_or_create_ledger(db, customer_id, USDT)
    idem = f"fee:{reference_id}"
    if (await db.execute(select(LedgerJournal).where(
        LedgerJournal.idempotency_key == idem
    ).with_for_update())).scalar_one_or_none():
        return

    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()

    available = D(str(fresh_ledger.available))
    reserved = D(str(fresh_ledger.trading_reserved))
    if available + reserved < fee_d and strict:
        raise ValueError("customer ledger cannot absorb trading fee")
    absorbable = min(available + reserved, fee_d)
    shortfall = fee_d - absorbable
    from_available = min(available, absorbable)
    from_reserved = absorbable - from_available
    fresh_ledger.available = available - from_available
    fresh_ledger.trading_reserved = reserved - from_reserved
    lines = []
    if from_available > 0:
        lines.append({"account_code": _customer_account(customer_id, "AVAILABLE"), "customer_id": customer_id, "debit": from_available, "credit": 0})
    if from_reserved > 0:
        lines.append({"account_code": _customer_account(customer_id, "TRADING_RESERVED"), "customer_id": customer_id, "debit": from_reserved, "credit": 0})
    if shortfall > 0:
        lines.append({"account_code": DEFICIT_ACCOUNT, "customer_id": customer_id, "debit": shortfall, "credit": 0})
    lines.append({"account_code": "EXPENSE:TRADING_FEES", "debit": 0, "credit": fee_d})
    await post_journal(db, currency=USDT, entry_type="TRADING_FEE", reference_type="TRADE",
                       reference_id=reference_id, idempotency_key=idem, lines=lines,
                       description="Trading fee attribution")
    if shortfall > 0:
        logger.critical("trading_fee_exceeds_balance customer=%s reference=%s fee=%s shortfall=%s",
                        customer_id, reference_id, fee_d, shortfall)
        await record_ledger_incident(
            db, key=f"CUSTOMER_DEFICIT:{reference_id}",
            summary="Trading fee exceeded the customer's available + reserved balance",
            detail={"customer_id": customer_id, "reference_id": reference_id, "fee": str(fee_d),
                    "shortfall": str(shortfall)}, customer_id=customer_id)


async def ledger_statement(db, customer_id: int, currency: str = USDT, limit: int = 100) -> list[dict]:
    rows = (await db.execute(select(LedgerJournal, LedgerJournalLine).join(
        LedgerJournalLine, LedgerJournalLine.journal_id == LedgerJournal.id
    ).where(LedgerJournalLine.customer_id == customer_id, LedgerJournal.currency == currency)
      .order_by(LedgerJournal.created_at.desc(), LedgerJournalLine.line_no).limit(limit))).all()
    return [{"journal_id": j.id, "entry_type": j.entry_type, "reference_type": j.reference_type,
             "reference_id": j.reference_id, "account_code": line.account_code,
             "debit": float(line.debit), "credit": float(line.credit),
             "created_at": j.created_at.isoformat() if j.created_at else None} for j, line in rows]


async def ledger_invariant_report(db, *, tolerance: str = "0.000001", limit: int = 50) -> dict:
    """Monitoring-only proof that the books agree with themselves. Never mutates balances.

    1. Every journal balances (sum(debit) == sum(credit)).
    2. Each customer's AVAILABLE / TRADING_RESERVED / WITHDRAWAL_RESERVED balance equals credits - debits posted to its
       liability account. A drift means some code path changed a balance without a journal (or the reverse).
    Opens a CRITICAL `LEDGER_INVARIANT_BREACH` incident when either fails.
    """
    tol = D(tolerance)
    unbalanced = (await db.execute(
        select(LedgerJournalLine.journal_id, func.sum(LedgerJournalLine.debit), func.sum(LedgerJournalLine.credit))
        .group_by(LedgerJournalLine.journal_id)
        .having(func.sum(LedgerJournalLine.debit) != func.sum(LedgerJournalLine.credit)).limit(limit))).all()
    net_rows = (await db.execute(
        select(LedgerJournalLine.customer_id, LedgerJournalLine.account_code,
               func.coalesce(func.sum(LedgerJournalLine.credit), 0) - func.coalesce(func.sum(LedgerJournalLine.debit), 0))
        .where(LedgerJournalLine.account_code.like("LIABILITY:CUSTOMER:%"))
        .group_by(LedgerJournalLine.customer_id, LedgerJournalLine.account_code))).all()
    journal_net: dict[tuple[int, str], Decimal] = {}
    for customer_id, account_code, net in net_rows:
        if customer_id is not None:
            journal_net[(int(customer_id), str(account_code).rsplit(":", 1)[-1])] = D(str(net))
    drifts = []
    ledgers = (await db.execute(select(CustomerLedgerAccount).where(CustomerLedgerAccount.currency == USDT))).scalars().all()
    for ledger in ledgers:
        for bucket, balance in (("AVAILABLE", ledger.available), ("TRADING_RESERVED", ledger.trading_reserved),
                                ("WITHDRAWAL_RESERVED", ledger.withdrawal_reserved)):
            diff = D(str(balance)) - journal_net.get((int(ledger.customer_id), bucket), D("0"))
            if abs(diff) > tol:
                drifts.append({"customer_id": int(ledger.customer_id), "bucket": bucket, "balance": str(D(str(balance))),
                               "journal_net": str(journal_net.get((int(ledger.customer_id), bucket), D("0"))), "drift": str(diff)})
    ok = not unbalanced and not drifts
    report = {"ok": ok, "unbalanced_journals": [{"journal_id": int(j), "debit": str(d), "credit": str(c)} for j, d, c in unbalanced],
              "bucket_drifts": drifts[:limit], "drift_count": len(drifts), "customers_checked": len(ledgers)}
    if not ok:
        logger.critical("ledger_invariant_breach unbalanced=%s drifts=%s", len(unbalanced), len(drifts))
        await record_ledger_incident(db, key="LEDGER_INVARIANT_BREACH", summary="Ledger invariant check failed: journals unbalanced or balances drifted from journals",
                                      detail={"unbalanced": len(unbalanced), "drifts": drifts[:10]}, customer_id=None)
    return report


_record_ledger_incident = record_ledger_incident  # backward-compatible alias
