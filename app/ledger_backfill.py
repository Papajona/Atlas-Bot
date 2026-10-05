"""Operator tool: journal a legacy opening balance for a customer whose ledger balance has no journal.

Background: before the fix in customer_funds.get_or_create_ledger(), legacy wallet balances were copied into
the ledger without a journal, so ledger_invariant_report() shows an AVAILABLE-bucket drift (balance > journal).
This module never guesses: it is dry-run by default, handles exactly one customer + the AVAILABLE bucket,
requires the operator to state the drift amount they reviewed, and refuses on any mismatch. A positive drift
can also be a *bug* that created money, so it must be reviewed per customer before it is journaled.
"""
from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import func, select

from .audit_chain import append_audit
from .customer_funds import LEGACY_MIGRATION_ACCOUNT, USDT, D, _customer_account, _q, post_journal
from .db import CustomerLedgerAccount, LedgerEntry, LedgerJournal, LedgerJournalLine

BUCKETS = ("AVAILABLE", "TRADING_RESERVED", "WITHDRAWAL_RESERVED")


async def compute_bucket_drifts(db, customer_id: int | None = None, currency: str = USDT) -> list[dict]:
    """Read-only (opens no incident). Same definition as ledger_invariant_report: balance - journal net."""
    q = (select(LedgerJournalLine.customer_id, LedgerJournalLine.account_code,
                func.coalesce(func.sum(LedgerJournalLine.credit), 0) - func.coalesce(func.sum(LedgerJournalLine.debit), 0))
         .where(LedgerJournalLine.account_code.like("LIABILITY:CUSTOMER:%"))
         .group_by(LedgerJournalLine.customer_id, LedgerJournalLine.account_code))
    if customer_id is not None:
        q = q.where(LedgerJournalLine.customer_id == customer_id)
    net = {}
    for cid, code, value in (await db.execute(q)).all():
        if cid is not None:
            net[(int(cid), str(code).rsplit(":", 1)[-1])] = D(str(value))
    lq = select(CustomerLedgerAccount).where(CustomerLedgerAccount.currency == currency)
    if customer_id is not None:
        lq = lq.where(CustomerLedgerAccount.customer_id == customer_id)
    drifts = []
    for ledger in (await db.execute(lq)).scalars().all():
        for bucket, balance in (("AVAILABLE", ledger.available), ("TRADING_RESERVED", ledger.trading_reserved),
                                ("WITHDRAWAL_RESERVED", ledger.withdrawal_reserved)):
            journal = net.get((int(ledger.customer_id), bucket), D("0"))
            diff = D(str(balance)) - journal
            if abs(diff) > D("0.000001"):
                drifts.append({"customer_id": int(ledger.customer_id), "bucket": bucket,
                               "balance": str(D(str(balance))), "journal_net": str(journal), "drift": str(diff)})
    return drifts


async def backfill_opening_balance(db, *, customer_id: int, expected_amount, operator: str, note: str,
                                   apply: bool = False, currency: str = USDT) -> dict:
    """Plan (apply=False) or post (apply=True) the opening-balance journal. Raises ValueError on any doubt."""
    if not str(operator or "").strip():
        raise ValueError("operator is required (it is written to the audit trail)")
    if not str(note or "").strip():
        raise ValueError("note is required (state how the drift was reviewed)")
    expected = _q(expected_amount)
    if expected <= 0:
        raise ValueError("expected_amount must be positive")
    drifts = await compute_bucket_drifts(db, customer_id, currency)
    by_bucket = {d["bucket"]: Decimal(d["drift"]) for d in drifts}
    if not by_bucket:
        raise ValueError("customer has no ledger drift; nothing to backfill")
    other = {b: v for b, v in by_bucket.items() if b != "AVAILABLE"}
    if other:
        raise ValueError(f"reserved-bucket drift present {other}; this tool only fixes the AVAILABLE bucket - investigate manually")
    drift = by_bucket.get("AVAILABLE", D("0"))
    if drift <= 0:
        raise ValueError(f"AVAILABLE drift is {drift} (not positive); a negative drift is a missing debit, not an opening balance")
    if _q(drift) != expected:
        raise ValueError(f"expected_amount {expected} does not match the actual drift {_q(drift)}; re-review before applying")
    idem = f"opening-backfill:{customer_id}:{currency}"
    if (await db.execute(select(func.count()).select_from(LedgerJournal).where(LedgerJournal.idempotency_key == idem))).scalar_one():
        raise ValueError("a backfill journal already exists for this customer")
    plan = {"customer_id": customer_id, "currency": currency, "amount": str(expected), "idempotency_key": idem,
            "applied": False}
    if not apply:
        return plan
    await post_journal(db, currency=currency, entry_type="OPENING_BALANCE_BACKFILL", reference_type="LEGACY_WALLET",
                       reference_id=f"customer:{customer_id}", idempotency_key=idem,
                       lines=[{"account_code": LEGACY_MIGRATION_ACCOUNT, "debit": expected, "credit": 0},
                              {"account_code": _customer_account(customer_id, "AVAILABLE", currency),
                               "customer_id": customer_id, "debit": 0, "credit": expected}],
                       description=f"Operator backfill of unjournaled legacy opening balance: {note.strip()[:200]}")
    db.add(LedgerEntry(customer_id=customer_id, currency=currency, entry_type="OPENING_BALANCE_BACKFILL", debit=0,
                       credit=expected, amount=expected, reference_type="LEGACY_WALLET",
                       reference_id=f"customer:{customer_id}", idempotency_key=idem,
                       metadata_json=json.dumps({"operator": operator.strip(), "note": note.strip()[:200]}, separators=(",", ":"))))
    await append_audit(db, event="LEDGER_OPENING_BALANCE_BACKFILL", actor_id=operator.strip(),
                       detail={"customer_id": customer_id, "currency": currency, "amount": str(expected), "note": note.strip()[:200]})
    await db.commit()
    plan["applied"] = True
    return plan
