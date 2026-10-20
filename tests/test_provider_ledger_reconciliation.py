import csv

import pytest

from scripts.verify_provider_ledger_reconciliation import (
    build_query,
    load_provider_statement,
    reconcile_rows,
    parse_utc,
)


def provider_row(provider_id="tx-001", amount="10.000000", currency="USDT", status="SUCCESS"):
    return {
        "provider_id": provider_id,
        "amount": amount,
        "currency": currency,
        "status": status,
        "event_time_utc": "2026-10-14T10:00:00Z",
    }


def db_row(
    provider_id="tx-001",
    amount="10.000000",
    currency="USDT",
    status="RELEASED",
    provider_status="SUCCESS",
    provider="sandbox-payout",
    reserve_count=1,
    settlement_count=1,
    release_count=0,
    journal_balance_failures=0,
):
    return {
        "request_id": "req-001",
        "provider_id": provider_id,
        "provider": provider,
        "amount": amount,
        "currency": currency,
        "status": status,
        "provider_status": provider_status,
        "reserve_count": reserve_count,
        "settlement_count": settlement_count,
        "release_count": release_count,
        "journal_balance_failures": journal_balance_failures,
    }


def test_successful_payout_matches_provider_amount_status_and_single_settlement():
    matched, unresolved, failures = reconcile_rows(
        [provider_row()],
        [db_row()],
        "sandbox-payout",
    )
    assert matched == 1
    assert unresolved == 0
    assert failures == []


@pytest.mark.parametrize(
    ("statement", "record"),
    [
        (provider_row(amount="10.01"), db_row(amount="10.00")),
        (provider_row(currency="BTC"), db_row(currency="USDT")),
        (provider_row(status="FAILED"), db_row(status="RELEASED", provider_status="SUCCESS")),
        (provider_row(), db_row(reserve_count=0)),
        (provider_row(), db_row(settlement_count=2)),
        (provider_row(), db_row(release_count=1)),
        (provider_row(), db_row(journal_balance_failures=1)),
        (provider_row(), db_row(provider="different-provider")),
    ],
)
def test_provider_ledger_mismatches_fail_closed(statement, record):
    matched, _, failures = reconcile_rows([statement], [record], "sandbox-payout")
    assert matched == 1
    assert failures


def test_failed_payout_requires_one_release_and_no_settlement():
    statement = provider_row(status="FAILED")
    record = db_row(
        status="FAILED",
        provider_status="FAILED",
        settlement_count=0,
        release_count=1,
    )
    matched, unresolved, failures = reconcile_rows([statement], [record], "sandbox-payout")
    assert matched == 1 and unresolved == 0
    assert failures == []


def test_pending_payout_must_not_have_terminal_journal():
    statement = provider_row(status="PENDING")
    record = db_row(
        status="UNKNOWN",
        provider_status="PENDING",
        settlement_count=0,
        release_count=0,
    )
    assert reconcile_rows([statement], [record], "sandbox-payout") == (1, 0, [])


def test_missing_provider_id_for_unknown_withdrawal_is_a_blocker():
    matched, unresolved, failures = reconcile_rows(
        [provider_row()],
        [db_row(provider_id="tx-001"), db_row(provider_id="", status="UNKNOWN")],
        "sandbox-payout",
    )
    assert matched == 1
    assert unresolved == 1
    assert any("no provider_id" in f for f in failures)


def test_completed_withdrawal_without_provider_id_is_still_a_blocker():
    matched, unresolved, failures = reconcile_rows(
        [provider_row()],
        [db_row(provider_id="", status="RELEASED")],
        "sandbox-payout",
    )
    assert matched == 0
    assert unresolved == 0
    assert any("cannot be reconciled" in item for item in failures)


def test_unmatched_statement_and_database_transactions_fail_closed():
    matched, _, failures = reconcile_rows(
        [provider_row(provider_id="external-only")],
        [db_row(provider_id="internal-only")],
        "sandbox-payout",
    )
    assert matched == 0
    assert any("unmatched provider_id" in f for f in failures)
    assert any("missing from provider statement" in f for f in failures)


def test_statement_parser_rejects_duplicate_ids_and_invalid_status(tmp_path):
    path = tmp_path / "provider.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["provider_id", "amount", "currency", "status", "event_time_utc"])
        writer.writeheader()
        writer.writerow(provider_row())
        writer.writerow(provider_row())
    with pytest.raises(ValueError, match="Duplicate provider_id"):
        load_provider_statement(path)

    path.write_text(
        "provider_id,amount,currency,status,event_time_utc\n"
        "tx-002,10,USDT,UNKNOWN_STATUS,2026-10-14T10:00:00Z\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unrecognized provider status"):
        load_provider_statement(path)


def test_query_window_requires_timezone_and_provider_is_restricted():
    start = parse_utc("2026-10-14T00:00:00Z", "start")
    end = parse_utc("2026-10-15T00:00:00Z", "end")
    query = build_query("sandbox-payout", start, end)
    assert "TIMESTAMPTZ" in query
    with pytest.raises(ValueError, match="Invalid provider"):
        build_query("'; DROP TABLE withdrawals;--", start, end)
    with pytest.raises(ValueError, match="UTC offset"):
        parse_utc("2026-10-14T00:00:00", "start")
