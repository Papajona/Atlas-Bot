from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cloud_sql_paper_role_bootstrap_is_non_privileged_and_target_database_bound():
    sql = (ROOT / "scripts" / "provision_cloud_sql_customer_paper_role.sql").read_text(encoding="utf-8")
    assert "\\set ON_ERROR_STOP on" in sql
    assert "CREATE ROLE atlas_customer_paper" in sql
    assert "LOGIN NOINHERIT NOBYPASSRLS" in sql
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION" in sql
    assert "GRANT CONNECT ON DATABASE %I TO atlas_customer_paper" in sql
    assert "current_database()" in sql
    assert "ALTER ROLE atlas_customer_paper SET default_transaction_read_only = off" in sql
    assert "ALTER ROLE atlas_customer_paper SET search_path = 'pg_catalog, customer_paper'" in sql
    assert "current_setting('transaction_read_only')" in sql
    assert "rolcreaterole" in sql
    assert "already exists" in sql
    assert "\\password atlas_customer_paper" in sql
    assert "PASSWORD '" not in sql
    assert "DROP ROLE" not in sql
    final_readback = sql.split("COMMIT;", 1)[1]
    assert "to_regnamespace('customer_paper') IS NOT NULL" in final_readback
    assert "has_schema_privilege('atlas_customer_paper', 'customer_paper'" not in final_readback



def test_cloud_sql_paper_role_and_schema_are_provisioned_as_separate_reviewed_steps():
    runbook = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    schema = (ROOT / "scripts" / "customer_paper_ledger_schema.sql").read_text(encoding="utf-8")
    assert "scripts/provision_cloud_sql_customer_paper_role.sql" in runbook
    assert "scripts/customer_paper_ledger_schema.sql" in runbook
    assert "scripts/provision_customer_paper_schema.py" in runbook
    assert "Do not use the Supabase-only" in runbook
    assert "REVOKE ALL ON SCHEMA public FROM %I" in schema
    assert "CREATE POLICY customer_paper_tenant" in schema
    assert "CREATE POLICY customer_paper_bar_budget_backend" in schema
    assert "grantor_role.rolname IN ('supabase_admin', 'cloudsqladmin')" in schema
