import csv
from pathlib import Path

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


def test_zero_activity_without_provider_attestation_fails_closed():
    matched, unresolved, failures = reconcile_rows([], [], "sandbox-payout")
    assert matched == 0
    assert unresolved == 0
    assert any(
        "zero activity cannot be independently verified" in failure
        for failure in failures
    )


def test_unattributed_withdrawal_journals_block_reconciliation_even_with_provider_rows():
    matched, unresolved, failures = reconcile_rows(
        [provider_row()],
        [db_row()],
        "sandbox-payout",
        withdrawal_journal_count=1,
        unattributed_withdrawal_journal_count=1,
    )
    assert matched == 1
    assert unresolved == 0
    assert any("without an attributable withdrawal record" in failure for failure in failures)



def test_other_provider_and_out_of_window_withdrawals_do_not_fail_selected_journal_count():
    matched, unresolved, failures = reconcile_rows(
        [provider_row()],
        [db_row()],
        "sandbox-payout",
        withdrawal_journal_count=1,
        unattributed_withdrawal_journal_count=0,
    )
    assert matched == 1
    assert unresolved == 0
    assert not any("withdrawal journal" in failure for failure in failures)


def test_orphan_journal_is_reported_once_even_with_scoped_journal_count():
    _matched, _unresolved, failures = reconcile_rows(
        [provider_row()],
        [db_row()],
        "sandbox-payout",
        withdrawal_journal_count=0,
        unattributed_withdrawal_journal_count=1,
    )
    orphan_failures = [
        failure for failure in failures if "withdrawal journal" in failure
    ]
    assert orphan_failures == [
        "withdrawal journals exist in the specified window without an attributable withdrawal record"
    ]



def test_zero_activity_statement_reports_one_independent_attestation_failure():
    _matched, _unresolved, failures = reconcile_rows([], [], "sandbox-payout")
    zero_activity_failures = [
        failure for failure in failures if "zero activity" in failure
    ]
    assert len(zero_activity_failures) == 1

def test_header_only_provider_export_is_rejected_without_provider_attestation(tmp_path):
    path = tmp_path / "provider-empty.csv"
    path.write_text(
        "provider_id,amount,currency,status,event_time_utc\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="zero-activity exports require independent provider-authenticated evidence"):
        load_provider_statement(path)


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



def test_provider_reconciliation_query_uses_read_only_audit_acl_contract():
    from datetime import datetime, timezone

    query = build_query(
        "sandbox-payout",
        datetime(2026, 10, 14, tzinfo=timezone.utc),
        datetime(2026, 10, 15, tzinfo=timezone.utc),
    )
    assert "'transaction_read_only', current_setting('transaction_read_only')" in query
    assert "'audit_role'" in query
    assert "'required_select_missing'" in query
    assert "'public_tables_with_write_privileges'" in query
    assert "'public_sequences_with_privileges'" in query
    assert "'public_functions_with_execute'" in query
    assert "'withdrawal_journal_count'" in query
    normalized_query = " ".join(query.split())
    assert "AND EXISTS ( SELECT 1 FROM matching_withdrawals w" in normalized_query
    assert "AND NOT EXISTS ( SELECT 1 FROM public.withdrawals w" in normalized_query
    assert "'unattributed_withdrawal_journal_count'" in query
    assert "j.reference_id IN (w.request_id, w.request_id || ':release')" in query
    assert "tables_missing_dml" not in query
    assert "public_tables_missing_dml" not in query
    assert "audit_role_failures(" in Path(
        "scripts/verify_provider_ledger_reconciliation.py"
    ).read_text(encoding="utf-8")

def test_provider_audit_reader_allowlist_includes_journal_timestamp_column():
    from scripts.verify_staging_restore import AUDIT_REQUIRED_SELECT_COLUMNS

    assert "created_at" in AUDIT_REQUIRED_SELECT_COLUMNS["ledger_journals"]
    provisioner = Path("scripts/provision_atlas_audit_reader.sql").read_text(encoding="utf-8")
    assert "reference_id, created_at)" in provisioner
    assert "('ledger_journals','created_at')" in provisioner


def test_query_window_requires_timezone_and_provider_is_restricted():
    start = parse_utc("2026-10-14T00:00:00Z", "start")
    end = parse_utc("2026-10-15T00:00:00Z", "end")
    query = build_query("sandbox-payout", start, end)
    assert "TIMESTAMPTZ" in query
    with pytest.raises(ValueError, match="Invalid provider"):
        build_query("'; DROP TABLE withdrawals;--", start, end)
    with pytest.raises(ValueError, match="UTC offset"):
        parse_utc("2026-10-14T00:00:00", "start")
