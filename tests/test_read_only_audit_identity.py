from scripts.verify_staging_restore import (
    AUDIT_REQUIRED_SELECT_COLUMNS,
    AUDIT_REQUIRED_SELECT_TABLES,
    audit_role_failures,
    read_database_uri,
)


PROJECT_REF = "wauaelzyugjuepmrewaw"
OTHER_REF = "abcdefghijklmnopqrst"


def valid_audit_role():
    return {
        "name": "atlas_audit_reader",
        "login": True,
        "inherit": False,
        "superuser": False,
        "create_role": False,
        "create_database": False,
        "replication": False,
        "bypass_rls": True,
        "role_default_read_only": True,
        "owns_public_tables": 0,
        "role_memberships": 0,
        "public_schema_usage": True,
        "public_schema_create": False,
        "required_select_missing": 0,
        "unexpected_select": 0,
        "public_tables_with_write_privileges": 0,
        "public_sequences_with_privileges": 0,
        "public_functions_with_execute": 0,
    }


def test_audit_identity_has_a_small_explicit_read_allowlist():
    assert AUDIT_REQUIRED_SELECT_TABLES == (
        "alembic_version",
        "customer_ledger_accounts",
        "ledger_entries",
        "ledger_journals",
        "ledger_journal_lines",
        "research_trial_archive",
        "research_trial_ledger_head",
        "research_holdout_registry",
        "withdrawals",
    )


def test_audit_role_contract_accepts_only_exact_read_only_identity():
    assert audit_role_failures(valid_audit_role(), "atlas_audit_reader", "on") == []


def test_audit_role_contract_rejects_all_write_paths_and_wider_reads():
    for key in (
        "public_tables_with_write_privileges",
        "public_sequences_with_privileges",
        "public_functions_with_execute",
        "unexpected_select",
        "owns_public_tables",
        "role_memberships",
    ):
        role = valid_audit_role()
        role[key] = 1
        assert audit_role_failures(role, "atlas_audit_reader", "on")


def test_audit_role_contract_rejects_wrong_identity_or_writable_transaction():
    assert audit_role_failures(valid_audit_role(), "atlas_paper_reader", "on")
    assert audit_role_failures(valid_audit_role(), "atlas_audit_reader", "off")


def test_read_database_uri_accepts_existing_supabase_session_pooler_format():
    env, host = read_database_uri(
        f"postgresql+asyncpg://atlas_audit_reader.{PROJECT_REF}:not-a-real-secret"
        "@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require",
        PROJECT_REF,
        OTHER_REF,
    )
    assert host == "aws-0-eu-west-1.pooler.supabase.com"
    assert env["PGUSER"] == f"atlas_audit_reader.{PROJECT_REF}"
    assert env["PGSSLMODE"] == "require"
    assert env["PGOPTIONS"] == "-c default_transaction_read_only=on -c statement_timeout=15000"


def test_read_database_uri_rejects_session_pooler_username_for_another_project():
    import pytest

    with pytest.raises(ValueError, match="does not match"):
        read_database_uri(
            "postgresql+asyncpg://atlas_audit_reader.otherprojectref000000:secret"
            "@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?ssl=require",
            PROJECT_REF,
            OTHER_REF,
        )


def test_read_database_uri_still_accepts_direct_supabase_endpoint():
    env, host = read_database_uri(
        f"postgresql+asyncpg://atlas_audit_reader:secret@db.{PROJECT_REF}.supabase.co:5432/postgres?ssl=require",
        PROJECT_REF,
        OTHER_REF,
    )
    assert host == f"db.{PROJECT_REF}.supabase.co"
    assert env["PGUSER"] == "atlas_audit_reader"

def test_audit_reader_column_allowlist_excludes_customer_profile_and_unneeded_withdrawal_data():
    assert AUDIT_REQUIRED_SELECT_COLUMNS["customer_ledger_accounts"] == (
        "available", "trading_reserved", "withdrawal_reserved"
    )
    assert AUDIT_REQUIRED_SELECT_COLUMNS["withdrawals"] == (
        "created_at", "request_id", "provider_id", "provider", "amount", "currency", "status", "provider_status"
    )
    assert "customer_id" not in AUDIT_REQUIRED_SELECT_COLUMNS["withdrawals"]
    assert "email" not in AUDIT_REQUIRED_SELECT_COLUMNS["withdrawals"]



def test_audit_role_contract_rejects_missing_role_owned_read_only_setting_even_when_session_is_read_only():
    role = valid_audit_role()
    role["role_default_read_only"] = False
    failures = audit_role_failures(role, "atlas_audit_reader", "on")
    assert any("default_transaction_read_only" in failure for failure in failures)


def test_audit_role_catalog_query_checks_role_and_database_default_read_only_setting():
    from scripts.verify_staging_restore import audit_role_observations_sql

    query = audit_role_observations_sql()
    assert "'role_default_read_only'" in query
    assert "pg_db_role_setting" in query
    assert "s.setrole = r.oid" in query
    assert "s.setdatabase" in query
    assert "default_transaction_read_only" in query
