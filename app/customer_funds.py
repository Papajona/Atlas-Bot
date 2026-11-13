from __future__ import annotations
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_UP
import json
import logging
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import AppState, CustomerLedgerAccount, Incident, LedgerEntry, LedgerJournal, LedgerJournalLine, SessionLocal, Wallet, utcnow

USDT = "USDT"
D = Decimal
logger = logging.getLogger(__name__)
LEGACY_MIGRATION_ACCOUNT = "ASSET:LEGACY:WALLET_MIGRATION"
_SCALE = Decimal("0.000001")  # matches the Numeric(38,6) ledger columns


def _q(value) -> Decimal:
    """Quantize to ledger precision (half-even) so journals and balances agree with the database."""
    return D(str(value)).quantize(_SCALE, rounding=ROUND_HALF_EVEN)


def _q_up(value) -> Decimal:
    """Quantize upward: reserves must never be short by a rounding remainder."""
    return D(str(value)).quantize(_SCALE, rounding=ROUND_UP)


async def record_ledger_incident(
    db, *, key: str, summary: str, detail: dict, customer_id: int | None,
    severity: str = "CRITICAL", reopen_resolved: bool = True,
) -> None:
    """Upsert an incident in the caller's transaction.

    Reopening resolved incidents remains the default for existing callers. Polling
    monitors that repeatedly observe the same historical ambiguity can set
    reopen_resolved=False so an operator's resolution is not undone by replay.
    """
    row = (await db.execute(select(Incident).where(Incident.incident_key == key).with_for_update())).scalar_one_or_none()
    payload = json.dumps(detail, default=str)
    if row:
        if not reopen_resolved and str(row.status).upper() in {"RESOLVED", "CLOSED"}:
            return
        row.severity = severity
        row.status = "OPEN"
        row.summary = summary[:500]
        row.detail_json = payload
        row.updated_at = utcnow()
        if reopen_resolved:
            row.resolved_at = None
            row.resolved_by = ""
        return
    db.add(Incident(incident_key=key, severity=severity, status="OPEN", category="LEDGER_RECONCILIATION",
                    customer_id=customer_id, summary=summary[:500], detail_json=payload))

async def get_or_create_ledger(db, customer_id: int, currency: str = USDT) -> CustomerLedgerAccount:
    row = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == currency,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
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
        ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if not row:
            raise
        return row
    # One-time migration of a positive legacy wallet balance into the authoritative ledger.
    # The opening balance is posted as a balanced journal in this same transaction so the
    # ledger balance can never exist without a corresponding immutable accounting record.
    wallet = (await db.execute(
        select(Wallet)
        .where(Wallet.customer_id == customer_id, Wallet.currency == currency)
        .with_for_update()
    )).scalar_one_or_none()
    opening = _q(wallet.available_balance or 0) if wallet else D("0")
    if wallet and opening > 0:
        row.available = opening
        row.trading_reserved = D("0")
        row.withdrawal_reserved = D("0")
        idem = f"opening:{customer_id}:{currency}"
        await post_journal(
            db,
            currency=currency,
            entry_type="OPENING_BALANCE",
            reference_type="LEGACY_WALLET",
            reference_id=f"wallet:{wallet.id}",
            idempotency_key=idem,
            lines=[
                {"account_code": "ASSET:LEGACY:WALLET_MIGRATION", "debit": opening, "credit": 0},
                {
                    "account_code": _customer_account(customer_id, "AVAILABLE", currency),
                    "customer_id": customer_id,
                    "debit": 0,
                    "credit": opening,
                },
            ],
            description="One-time migration of legacy wallet balance into the authoritative ledger",
        )
        # Preserve the legacy LedgerEntry stream used by existing reporting/reconciliation.
        # It has its own idempotency constraint, so the same stable key cannot double-post.
        existing_entry = (
            await db.execute(
                select(LedgerEntry).where(LedgerEntry.idempotency_key == idem)
            )
        ).scalar_one_or_none()
        if existing_entry is None:
            db.add(
                LedgerEntry(
                    customer_id=customer_id,
                    currency=currency,
                    entry_type="OPENING_BALANCE",
                    debit=0,
                    credit=opening,
                    amount=opening,
                    reference_type="LEGACY_WALLET",
                    reference_id=f"wallet:{wallet.id}",
                    idempotency_key=idem,
                    metadata_json=json.dumps(
                        {"source": "legacy_wallet_available_balance"},
                        separators=(",", ":"),
                    ),
                )
            )
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
    await get_or_create_ledger(db, customer_id, USDT)
    legacy_idem = f"deposit:{provider_reference}"
    idem = f"deposit:{provider}:{provider_reference}" if provider else legacy_idem
    keys = {idem, legacy_idem}
    existing = (await db.execute(select(LedgerEntry).where(LedgerEntry.idempotency_key.in_(keys)))).scalars().first()
    if existing:
        # Idempotency is not permission to reuse another customer's transaction or
        # silently accept the same provider reference with a different amount.
        if (
            existing.customer_id != customer_id
            or str(existing.currency or "").upper() != USDT
            or existing.entry_type != "DEPOSIT_CREDIT"
            or D(str(existing.amount or 0)) != amount_d
        ):
            raise ValueError("deposit idempotency key already belongs to a different customer, currency, type, or amount")
        return await get_or_create_ledger(db, customer_id, USDT)
    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()
    # The initial idempotency lookup is only a fast path. A concurrent deposit can
    # commit while this transaction waits for the customer ledger lock, so the key
    # must be rechecked while holding the authoritative lock before crediting funds.
    existing = (await db.execute(select(LedgerEntry).where(
        LedgerEntry.idempotency_key.in_(keys)
    ).with_for_update())).scalars().first()
    if existing:
        if existing.customer_id != customer_id or D(str(existing.amount)) != amount_d:
            raise ValueError("deposit idempotency key already belongs to a different deposit")
        return fresh_ledger
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
        wallet.available_balance = D(str(ledger.available))
        wallet.locked_balance = D(str(ledger.trading_reserved + ledger.withdrawal_reserved))
        wallet.updated_at = utcnow()

async def _check_reserve_replay(db, journal: LedgerJournal, customer_id: int, amount_d: Decimal, bucket: str) -> None:
    """Validate that an idempotent reserve replay matches its original customer and amount."""
    lines = (await db.execute(select(LedgerJournalLine).where(
        LedgerJournalLine.journal_id == journal.id,
        LedgerJournalLine.account_code == _customer_account(customer_id, bucket),
    ))).scalars().all()
    if not lines:
        raise ValueError("idempotency key already used by a different customer")
    recorded = sum((D(str(line.credit)) for line in lines), D("0"))
    if recorded != amount_d:
        logger.critical(
            "reserve_replay_amount_mismatch customer=%s key=%s recorded=%s requested=%s",
            customer_id, journal.idempotency_key, recorded, amount_d,
        )
        await record_ledger_incident(
            db,
            key=f"RESERVE_REPLAY_MISMATCH:{journal.idempotency_key}",
            summary="Idempotent reserve replayed with a different amount",
            detail={"customer_id": customer_id, "idempotency_key": journal.idempotency_key,
                    "recorded": str(recorded), "requested": str(amount_d)},
            customer_id=customer_id,
            severity="HIGH",
        )
        raise ValueError("idempotency key already used with a different reserve amount")


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
    existing_reserve = (await db.execute(select(LedgerJournal).where(
        LedgerJournal.idempotency_key == idem
    ).with_for_update())).scalar_one_or_none()
    if existing_reserve:
        await _check_reserve_replay(db, existing_reserve, customer_id, amount_d, "TRADING_RESERVED")
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

async def release_trading(
    db, customer_id: int, amount: float, *,
    reference_id: str, full_allocation: bool = False,
) -> CustomerLedgerAccount:
    # A fully released allocation must unwind the same upward-rounded amount
    # that reserve_trading() booked. Partial releases stay half-even to avoid
    # releasing more collateral than the requested partial amount.
    amount_d = _q_up(amount) if full_allocation and D(str(amount)) > 0 else _q(amount)
    await get_or_create_ledger(db, customer_id, USDT)
    idem = f"release:{reference_id}:{customer_id}"
    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()
    # Re-check idempotency after acquiring the authoritative ledger row lock.
    # A concurrent caller may have committed the journal while this transaction waited.
    existing = (
        await db.execute(
            select(LedgerJournal)
            .where(LedgerJournal.idempotency_key == idem)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing:
        await _validate_withdrawal_action_replay(
            db, existing, customer_id=customer_id, amount_d=amount_d,
            reference_id=reference_id, action="settle",
        )
        return fresh_ledger
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
    existing_reserve = (await db.execute(select(LedgerJournal).where(
        LedgerJournal.idempotency_key == idem
    ).with_for_update())).scalar_one_or_none()
    if existing_reserve:
        await _check_reserve_replay(db, existing_reserve, customer_id, amount_d, "WITHDRAWAL_RESERVED")
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

async def _validate_withdrawal_action_replay(
    db,
    journal: LedgerJournal,
    *,
    customer_id: int,
    amount_d: Decimal,
    reference_id: str,
    action: str,
) -> None:
    """Reject a reused withdrawal idempotency key whose original operation differs.

    Journal uniqueness prevents duplicate postings, but a duplicate key alone is
    not proof that the retry describes the same customer, withdrawal or amount.
    """
    expected_entry_type = (
        "WITHDRAWAL_RELEASE" if action == "release" else "WITHDRAWAL_SETTLEMENT"
    )
    problems = []
    if (
        journal.entry_type != expected_entry_type
        or str(journal.currency or "").upper() != USDT
        or journal.reference_type != "WITHDRAWAL"
        or journal.reference_id != reference_id
    ):
        problems.append("journal identity")

    entry = (
        await db.execute(
            select(LedgerEntry)
            .where(LedgerEntry.idempotency_key == journal.idempotency_key)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if entry is None:
        problems.append("missing ledger entry")
        metadata = {}
    else:
        if (
            entry.customer_id != customer_id
            or str(entry.currency or "").upper() != USDT
            or entry.entry_type != expected_entry_type
            or entry.reference_type != "WITHDRAWAL"
            or entry.reference_id != reference_id
        ):
            problems.append("ledger entry identity")
        try:
            metadata = json.loads(entry.metadata_json or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except (TypeError, ValueError):
            metadata = {}
            problems.append("invalid replay metadata")
        # Older journals did not persist the caller's requested amount separately.
        # Their recorded amount is used conservatively; a mismatch fails closed.
        recorded_request = metadata.get("requested_amount", entry.amount)
        try:
            if _q(recorded_request) != amount_d:
                problems.append("requested amount")
        except (TypeError, ValueError, ArithmeticError):
            problems.append("invalid recorded amount")

    lines = (
        await db.execute(
            select(LedgerJournalLine).where(
                LedgerJournalLine.journal_id == journal.id
            )
        )
    ).scalars().all()
    if action == "release":
        reserved_debit = sum(
            (
                D(str(line.debit))
                for line in lines
                if line.account_code == _customer_account(customer_id, "WITHDRAWAL_RESERVED")
            ),
            D("0"),
        )
        available_credit = sum(
            (
                D(str(line.credit))
                for line in lines
                if line.account_code == _customer_account(customer_id, "AVAILABLE")
            ),
            D("0"),
        )
        if (
            entry is None
            or reserved_debit != D(str(entry.amount))
            or available_credit != D(str(entry.amount))
            or reserved_debit <= 0
        ):
            problems.append("release journal lines")
    else:
        custody_credit = sum(
            (
                D(str(line.credit))
                for line in lines
                if line.account_code == "ASSET:TRON:WITHDRAWAL"
            ),
            D("0"),
        )
        customer_line_present = any(
            line.customer_id == customer_id
            and line.account_code
            in {
                _customer_account(customer_id, "WITHDRAWAL_RESERVED"),
                DEFICIT_ACCOUNT,
            }
            for line in lines
        )
        if (
            entry is None
            or custody_credit != amount_d
            or D(str(entry.amount)) != amount_d
            or not customer_line_present
        ):
            problems.append("settlement journal lines")

    if problems:
        await record_ledger_incident(
            db,
            key=f"WITHDRAWAL_REPLAY_MISMATCH:{journal.idempotency_key}",
            summary="Withdrawal idempotency key replayed with a different or inconsistent operation",
            detail={
                "action": action,
                "customer_id": customer_id,
                "reference_id": reference_id,
                "requested_amount": str(amount_d),
                "idempotency_key": journal.idempotency_key,
                "problems": problems,
            },
            customer_id=customer_id,
            severity="HIGH",
        )
        raise ValueError(
            "withdrawal idempotency key already used with a different or inconsistent operation"
        )


async def release_withdrawal(db, customer_id: int, amount: float, *, reference_id: str) -> CustomerLedgerAccount:
    amount_d = _q(amount)
    if amount_d <= 0:
        raise ValueError("withdrawal release amount must be positive")
    await get_or_create_ledger(db, customer_id, USDT)
    idem = f"withdrawal-release:{reference_id}"
    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()
    existing = (
        await db.execute(
            select(LedgerJournal)
            .where(LedgerJournal.idempotency_key == idem)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing:
        await _validate_withdrawal_action_replay(
            db, existing, customer_id=customer_id, amount_d=amount_d,
            reference_id=reference_id, action="release",
        )
        return fresh_ledger
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
    db.add(LedgerEntry(
        customer_id=customer_id, currency=USDT, entry_type="WITHDRAWAL_RELEASE",
        debit=0, credit=release, amount=release, reference_type="WITHDRAWAL",
        reference_id=reference_id, idempotency_key=idem,
        metadata_json=json.dumps(
            {"requested_amount": format(amount_d, "f"), "effective_amount": format(release, "f")},
            separators=(",", ":"),
        ),
    ))
    return fresh_ledger

async def settle_withdrawal(db, customer_id: int, amount: float, *, reference_id: str) -> CustomerLedgerAccount:
    amount_d = _q(amount)
    if amount_d <= 0:
        raise ValueError("withdrawal settlement amount must be positive")
    ledger = await get_or_create_ledger(db, customer_id, USDT)
    idem = f"withdrawal-settlement:{reference_id}"
    existing = (
        await db.execute(
            select(LedgerJournal).where(LedgerJournal.idempotency_key == idem)
        )
    ).scalar_one_or_none()
    if existing:
        await _validate_withdrawal_action_replay(
            db, existing, customer_id=customer_id, amount_d=amount_d,
            reference_id=reference_id, action="settle",
        )
        return ledger
    fresh_ledger = (await db.execute(select(CustomerLedgerAccount).where(
        CustomerLedgerAccount.customer_id == customer_id,
        CustomerLedgerAccount.currency == USDT,
    ).with_for_update())).scalar_one()
    # The pre-lock idempotency check is only a fast path. A concurrent settlement can
    # commit while this transaction waits on the customer ledger row. Re-check after
    # acquiring the authoritative row lock or a retry could create a second payout journal.
    existing = (
        await db.execute(
            select(LedgerJournal)
            .where(LedgerJournal.idempotency_key == idem)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing:
        await _validate_withdrawal_action_replay(
            db, existing, customer_id=customer_id, amount_d=amount_d,
            reference_id=reference_id, action="settle",
        )
        return fresh_ledger
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
        await _engage_kill_switch_for_deficit(db, customer_id=customer_id, reference_id=reference_id,
                                               shortfall=shortfall, source="WITHDRAWAL_SETTLEMENT")
    shortfall = amount_d - settle  # 0 unless the reserve was short
    if settle <= 0 and shortfall <= 0:
        return fresh_ledger
    if settle > 0:
        fresh_ledger.withdrawal_reserved = D(str(fresh_ledger.withdrawal_reserved)) - settle
    # The full payout left custody, so the journal must credit the full amount. Any part the
    # customer's reserve could not cover is booked to the customer-deficit receivable (same
    # treatment as trading losses) instead of leaving the books short by shortfall.
    lines = []
    if settle > 0:
        lines.append({"account_code": _customer_account(customer_id, "WITHDRAWAL_RESERVED"), "customer_id": customer_id, "debit": settle, "credit": 0})
    if shortfall > 0:
        lines.append({"account_code": DEFICIT_ACCOUNT, "customer_id": customer_id, "debit": shortfall, "credit": 0})
    lines.append({"account_code": "ASSET:TRON:WITHDRAWAL", "debit": 0, "credit": amount_d})
    await post_journal(db, currency=USDT, entry_type="WITHDRAWAL_SETTLEMENT", reference_type="WITHDRAWAL",
                       reference_id=reference_id, idempotency_key=idem, lines=lines,
                       description="Settle completed customer withdrawal")
    db.add(LedgerEntry(
        customer_id=customer_id, currency=USDT, entry_type="WITHDRAWAL_SETTLEMENT",
        debit=amount_d, credit=0, amount=amount_d, reference_type="WITHDRAWAL",
        reference_id=reference_id, idempotency_key=idem,
        metadata_json=json.dumps(
            {
                "requested_amount": format(amount_d, "f"),
                "settled_from_reserve": format(settle, "f"),
                "shortfall": format(shortfall, "f"),
            },
            separators=(",", ":"),
        ),
    ))
    return fresh_ledger

async def customer_balance(db, customer_id: int, currency: str = USDT, *, lock: bool = False) -> dict:
    """Read ledger balances while retaining exact wire values alongside legacy numeric values."""
    ledger = None
    if not lock:
        ledger = (await db.execute(select(CustomerLedgerAccount).where(
            CustomerLedgerAccount.customer_id == customer_id,
            CustomerLedgerAccount.currency == currency,
        ))).scalar_one_or_none()
    if ledger is None:
        ledger = await get_or_create_ledger(db, customer_id, currency)

    available = D(str(ledger.available))
    trading_reserved = D(str(ledger.trading_reserved))
    withdrawal_reserved = D(str(ledger.withdrawal_reserved))
    total = available + trading_reserved + withdrawal_reserved
    # Keep numeric aliases for in-process risk calculations. Public API handlers
    # must use customer_balance_api(), which emits the exact Decimal strings.
    return {
        "available": float(available),
        "trading_reserved": float(trading_reserved),
        "withdrawal_reserved": float(withdrawal_reserved),
        "total": float(total),
        "available_exact": format(available, "f"),
        "trading_reserved_exact": format(trading_reserved, "f"),
        "withdrawal_reserved_exact": format(withdrawal_reserved, "f"),
        "total_exact": format(total, "f"),
    }


def customer_balance_api(balance: dict) -> dict[str, str | int]:
    """Serialize customer ledger balances as exact decimal strings and integer micro-USDT.

    In-process risk code uses customer_balance(); this API serializer never emits a
    binary float for the authoritative ledger buckets. UI clients may parse the decimal
    strings for display but must use strings/minor units for comparisons and round-trips.
    """
    available = D(str(balance.get("available_exact", balance["available"])))
    trading_reserved = D(str(balance.get("trading_reserved_exact", balance["trading_reserved"])))
    withdrawal_reserved = D(str(balance.get("withdrawal_reserved_exact", balance["withdrawal_reserved"])))
    total = D(str(balance.get("total_exact", balance["total"])))
    scale = Decimal("1000000")  # ledger Numeric(38, 6) precision
    locked = trading_reserved + withdrawal_reserved
    available_text = format(available, "f")
    trading_text = format(trading_reserved, "f")
    withdrawal_text = format(withdrawal_reserved, "f")
    total_text = format(total, "f")
    locked_text = format(locked, "f")
    return {
        # Primary public amount fields are exact decimal strings, not JSON floats.
        "available": available_text,
        "trading_reserved": trading_text,
        "withdrawal_reserved": withdrawal_text,
        "total": total_text,
        "locked": locked_text,
        # Explicitly named aliases remain for client migrations and reconciliation.
        "available_exact": available_text,
        "trading_reserved_exact": trading_text,
        "withdrawal_reserved_exact": withdrawal_text,
        "total_exact": total_text,
        "locked_exact": locked_text,
        "available_minor_units": int(available * scale),
        "trading_reserved_minor_units": int(trading_reserved * scale),
        "withdrawal_reserved_minor_units": int(withdrawal_reserved * scale),
        "total_minor_units": int(total * scale),
        "locked_minor_units": int(locked * scale),
        "minor_units_scale": 1_000_000,
    }


DEFICIT_ACCOUNT = "ASSET:RECEIVABLE:CUSTOMER_DEFICIT"


async def _engage_kill_switch_for_deficit(db, *, customer_id: int, reference_id: str, shortfall: Decimal, source: str) -> None:
    """Stop new live risk when a post-trade deficit is booked; never omit actual losses."""
    threshold = D(str(settings.customer_deficit_limit_usdt))
    if not threshold.is_finite() or threshold <= 0:
        state = await db.get(AppState, 1)
        if state is not None:
            state.kill_switch = True
            state.live_enabled = False
        raise RuntimeError("CUSTOMER_DEFICIT_LIMIT_USDT must be finite and > 0")
    # Any realized customer deficit is unsafe: engage the live kill switch for
    # every positive shortfall. The configured threshold is recorded for severity/
    # escalation telemetry, never used to permit continued live trading in deficit.
    threshold_reached = shortfall >= threshold
    state = await db.get(AppState, 1)
    if shortfall > 0 and state is not None:
        state.kill_switch = True
        state.live_enabled = False
    await record_ledger_incident(
        db,
        key=f"CUSTOMER_DEFICIT_LIMIT:{customer_id}:{reference_id}",
        summary="Customer deficit recorded; live trading halted for any positive shortfall",
        detail={"customer_id": customer_id, "reference_id": reference_id,
                "shortfall": str(shortfall), "source": source,
                "threshold_usdt": str(threshold), "threshold_reached": threshold_reached,
                "action": "KILL_SWITCH_ENGAGED_FOR_ANY_POSITIVE_DEFICIT"},
        customer_id=customer_id,
        severity="CRITICAL",
    )


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
    await get_or_create_ledger(db, customer_id, USDT)
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
        await _engage_kill_switch_for_deficit(db, customer_id=customer_id, reference_id=reference_id,
                                               shortfall=shortfall, source="REALIZED_TRADING_LOSS")


async def settle_trading_fee(db, *, customer_id: int, fee: float, reference_id: str, strict: bool = True) -> None:
    """Debit a trading fee from the customer's liability buckets exactly once.

    ``strict=False`` is for fill handling (see settle_realized_pnl): absorb what the customer
    has, journal the remainder to the customer-deficit receivable and open a CRITICAL incident.
    """
    fee_d = _q(fee)
    if fee_d <= 0:
        return
    await get_or_create_ledger(db, customer_id, USDT)
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
        await _engage_kill_switch_for_deficit(db, customer_id=customer_id, reference_id=reference_id,
                                               shortfall=shortfall, source="TRADING_FEE")


async def ledger_statement(
    db,
    customer_id: int,
    currency: str = USDT,
    limit: int | None = 100,
    *,
    cursor_journal_id: int | None = None,
    cursor_line_no: int | None = None,
    max_journal_id: int | None = None,
) -> list[dict]:
    """Return a bounded customer journal page using an index-friendly keyset cursor."""
    if (cursor_journal_id is None) != (cursor_line_no is None):
        raise ValueError("ledger statement keyset cursor requires both journal ID and line number")
    statement = select(LedgerJournal, LedgerJournalLine).join(
        LedgerJournalLine, LedgerJournalLine.journal_id == LedgerJournal.id
    ).where(
        LedgerJournalLine.customer_id == customer_id,
        LedgerJournal.currency == currency,
    )
    if max_journal_id is not None:
        statement = statement.where(LedgerJournal.id <= max_journal_id)
    if cursor_journal_id is not None:
        statement = statement.where(or_(
            LedgerJournal.id < cursor_journal_id,
            and_(
                LedgerJournal.id == cursor_journal_id,
                LedgerJournalLine.line_no > cursor_line_no,
            ),
        ))
    statement = statement.order_by(
        LedgerJournal.id.desc(),
        LedgerJournalLine.line_no.asc(),
    )
    if limit is not None:
        statement = statement.limit(limit)
    rows = (await db.execute(statement)).all()
    return [{
        "journal_id": j.id, "line_no": line.line_no,
        "entry_type": j.entry_type, "reference_type": j.reference_type,
        "reference_id": j.reference_id, "account_code": line.account_code,
        # Ledger statement amounts are serialized as exact decimal strings, never floats.
        "debit": format(D(str(line.debit)), "f"),
        "credit": format(D(str(line.credit)), "f"),
        "debit_exact": format(D(str(line.debit)), "f"),
        "credit_exact": format(D(str(line.credit)), "f"),
        "debit_minor_units": int(D(str(line.debit)) * Decimal("1000000")),
        "credit_minor_units": int(D(str(line.credit)) * Decimal("1000000")),
        "minor_units_scale": 1_000_000,
        "created_at": j.created_at.isoformat() if j.created_at else None,
    } for j, line in rows]


async def ledger_statement_export_bounds(
    db,
    customer_id: int,
    currency: str = USDT,
    *,
    max_journal_id: int | None = None,
) -> tuple[int, int | None]:
    """Capture the export row count and optionally verify it under the same ID ceiling."""
    statement = (
        select(func.count(), func.max(LedgerJournal.id))
        .select_from(LedgerJournal)
        .join(LedgerJournalLine, LedgerJournalLine.journal_id == LedgerJournal.id)
        .where(
            LedgerJournalLine.customer_id == customer_id,
            LedgerJournal.currency == currency,
        )
    )
    if max_journal_id is not None:
        statement = statement.where(LedgerJournal.id <= max_journal_id)
    row_count, observed_max_id = (await db.execute(statement)).one()
    return int(row_count or 0), (
        int(observed_max_id) if observed_max_id is not None else None
    )


async def _invariant_snapshot(db, tol: Decimal, customer_id: int | None = None):
    """Compute a global or one-customer invariant snapshot in one transaction."""
    customer_journal_ids = None
    customer_line_filter = None
    if customer_id is not None:
        customer_id = int(customer_id)
        customer_account_prefix = f"LIABILITY:CUSTOMER:{customer_id}:%"
        # Match both independent customer identifiers. This captures a journal
        # even when one side has a customer_id/account-code mismatch.
        customer_line_filter = or_(
            LedgerJournalLine.customer_id == customer_id,
            LedgerJournalLine.account_code.like(customer_account_prefix),
        )
        customer_journal_ids = (
            select(LedgerJournalLine.journal_id)
            .where(customer_line_filter)
            .distinct()
        )
    unbalanced_statement = (
        select(LedgerJournalLine.journal_id, func.sum(LedgerJournalLine.debit), func.sum(LedgerJournalLine.credit))
        .group_by(LedgerJournalLine.journal_id)
        .having(func.sum(LedgerJournalLine.debit) != func.sum(LedgerJournalLine.credit))
    )
    if customer_journal_ids is not None:
        unbalanced_statement = unbalanced_statement.where(
            LedgerJournalLine.journal_id.in_(customer_journal_ids)
        )
    unbalanced = (await db.execute(unbalanced_statement)).all()
    net_statement = (
        select(LedgerJournalLine.customer_id, LedgerJournalLine.account_code,
               func.coalesce(func.sum(LedgerJournalLine.credit), 0) - func.coalesce(func.sum(LedgerJournalLine.debit), 0))
        .where(LedgerJournalLine.account_code.like("LIABILITY:CUSTOMER:%"))
    )
    if customer_line_filter is not None:
        net_statement = net_statement.where(customer_line_filter)
    net_rows = (await db.execute(
        net_statement.group_by(LedgerJournalLine.customer_id, LedgerJournalLine.account_code)
    )).all()
    journal_net: dict[tuple[int, str], Decimal] = {}
    for journal_customer_id, account_code, net in net_rows:
        if journal_customer_id is not None:
            journal_net[(int(journal_customer_id), str(account_code).rsplit(":", 1)[-1])] = D(str(net))

    # The customer_id column and the customer embedded in LIABILITY account codes
    # are independent persisted values. Reuse the grouped net_rows query so this
    # integrity check does not issue a second full-history DISTINCT scan.
    customer_account_mismatches = []
    for line_customer_id, account_code, net in net_rows:
        if line_customer_id is not None:
            journal_net[(int(line_customer_id), str(account_code).rsplit(":", 1)[-1])] = D(str(net))

        parts = str(account_code or "").split(":")
        embedded_customer_id = None
        if len(parts) == 5 and parts[:2] == ["LIABILITY", "CUSTOMER"]:
            try:
                embedded_customer_id = int(parts[2])
            except (TypeError, ValueError):
                embedded_customer_id = None
        if line_customer_id is None or embedded_customer_id is None or int(line_customer_id) != embedded_customer_id:
            customer_account_mismatches.append({
                "journal_customer_id": int(line_customer_id) if line_customer_id is not None else None,
                "account_code": str(account_code),
                "account_customer_id": embedded_customer_id,
            })
    customer_account_mismatches.sort(
        key=lambda item: (
            item["account_code"],
            -1 if item["journal_customer_id"] is None else item["journal_customer_id"],
        )
    )
    drifts = []
    ledger_statement = select(CustomerLedgerAccount).where(CustomerLedgerAccount.currency == USDT)
    if customer_id is not None:
        ledger_statement = ledger_statement.where(CustomerLedgerAccount.customer_id == customer_id)
    ledgers = (await db.execute(ledger_statement)).scalars().all()
    for ledger in ledgers:
        for bucket, balance in (
            ("AVAILABLE", ledger.available),
            ("TRADING_RESERVED", ledger.trading_reserved),
            ("WITHDRAWAL_RESERVED", ledger.withdrawal_reserved),
        ):
            diff = D(str(balance)) - journal_net.get((int(ledger.customer_id), bucket), D("0"))
            if abs(diff) > tol:
                drifts.append({
                    "customer_id": int(ledger.customer_id),
                    "bucket": bucket,
                    "balance": str(D(str(balance))),
                    "journal_net": str(journal_net.get((int(ledger.customer_id), bucket), D("0"))),
                    "drift": str(diff),
                })
    return unbalanced, drifts, customer_account_mismatches, len(ledgers)


def _dialect_name(db) -> str:
    try:
        return str(db.get_bind().dialect.name)
    except Exception:
        try:
            return str(db.bind.dialect.name)
        except Exception:
            return ""


async def ledger_invariant_report(
    db,
    *,
    tolerance: str = "0.000001",
    limit: int = 50,
    customer_id: int | None = None,
) -> dict:
    """Monitoring-only proof that the books agree with themselves.

    Pass customer_id to reconcile one customer without mixing unrelated customers
    from a shared test database or a multi-tenant monitoring job.

    On PostgreSQL, the invariant reads use a separate REPEATABLE READ transaction so a
    concurrent commit cannot produce a false drift by mixing snapshots.
    """
    tol = D(tolerance)
    if _dialect_name(db) == "postgresql":
        async with SessionLocal() as snap:
            if _dialect_name(snap) == "postgresql":
                await snap.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                unbalanced, drifts, customer_account_mismatches, checked = await _invariant_snapshot(
                    snap, tol, customer_id=customer_id
                )
                await snap.rollback()
            else:
                # Unit tests may pass an isolated database session while the
                # application-wide SessionLocal points at a different dialect.
                # Never apply PostgreSQL isolation options to a non-PostgreSQL engine.
                unbalanced, drifts, customer_account_mismatches, checked = await _invariant_snapshot(
                    db, tol, customer_id=customer_id
                )
    else:
        unbalanced, drifts, customer_account_mismatches, checked = await _invariant_snapshot(
            db, tol, customer_id=customer_id
        )

    unbalanced = unbalanced[:limit]
    ok = not unbalanced and not drifts and not customer_account_mismatches
    report = {
        "ok": ok,
        "unbalanced_journals": [
            {"journal_id": int(j), "debit": str(d), "credit": str(cr)}
            for j, d, cr in unbalanced
        ],
        "bucket_drifts": drifts[:limit],
        "drift_count": len(drifts),
        "customer_account_mismatches": customer_account_mismatches[:limit],
        "customer_account_mismatch_count": len(customer_account_mismatches),
        "customers_checked": checked,
    }
    if not ok:
        logger.critical(
            "ledger_invariant_breach unbalanced=%s drifts=%s customer_account_mismatches=%s",
            len(unbalanced), len(drifts), len(customer_account_mismatches),
        )
        await record_ledger_incident(
            db,
            key=(f"LEDGER_INVARIANT_BREACH:{int(customer_id)}" if customer_id is not None else "LEDGER_INVARIANT_BREACH"),
            summary=(
                f"Ledger invariant check failed for customer {int(customer_id)}"
                if customer_id is not None
                else "Ledger invariant check failed: journals unbalanced or balances drifted from journals"
            ),
            detail={
                "unbalanced": len(unbalanced),
                "drifts": drifts[:10],
                "customer_account_mismatches": customer_account_mismatches[:10],
            },
            customer_id=int(customer_id) if customer_id is not None else None,
        )
    return report


_record_ledger_incident = record_ledger_incident  # backward-compatible alias
