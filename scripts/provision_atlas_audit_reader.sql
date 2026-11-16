\set ON_ERROR_STOP on

-- Dedicated read-only identity for restore and provider-ledger audits.
-- Apply only after alembic is at 0059_research_integrity_controls and after a
-- recoverable backup has been independently verified. Never reuse an existing role.
BEGIN;

DO $guard$
DECLARE
    missing_tables text[];
    unprotected_tables integer;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'atlas_audit_reader') THEN
        RAISE EXCEPTION 'atlas_audit_reader already exists; inspect its attributes/grants rather than reusing it';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolcreaterole) THEN
        RAISE EXCEPTION 'current role cannot provision the dedicated audit login';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.alembic_version
                   WHERE version_num = '0059_research_integrity_controls') THEN
        RAISE EXCEPTION 'database must be at 0059_research_integrity_controls before audit role provisioning';
    END IF;

    SELECT array_agg(required.table_name ORDER BY required.table_name)
      INTO missing_tables
    FROM unnest(ARRAY[
        'alembic_version','customer_ledger_accounts','ledger_entries','ledger_journals',
        'ledger_journal_lines','research_trial_archive','research_trial_ledger_head',
        'research_holdout_registry','withdrawals'
    ]::text[]) AS required(table_name)
    WHERE to_regclass(format('public.%I', required.table_name)) IS NULL;
    IF coalesce(array_length(missing_tables, 1), 0) > 0 THEN
        RAISE EXCEPTION 'required audit tables are missing: %', missing_tables;
    END IF;

    SELECT count(*) INTO unprotected_tables
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT c.relrowsecurity;
    IF unprotected_tables <> 0 THEN
        RAISE EXCEPTION 'public RLS lockdown is incomplete; refusing to provision audit identity';
    END IF;
END
$guard$;

CREATE ROLE atlas_audit_reader
    LOGIN NOINHERIT BYPASSRLS
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

GRANT CONNECT ON DATABASE postgres TO atlas_audit_reader;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM atlas_audit_reader;
GRANT USAGE ON SCHEMA public TO atlas_audit_reader;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM atlas_audit_reader;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM atlas_audit_reader;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM atlas_audit_reader;

-- Column-level allowlist shared by restore and provider reconciliation verifiers.
-- Do not grant table-level SELECT: unrelated customer/profile/payout columns remain inaccessible.
GRANT SELECT (version_num) ON TABLE public.alembic_version TO atlas_audit_reader;
GRANT SELECT (available, trading_reserved, withdrawal_reserved)
    ON TABLE public.customer_ledger_accounts TO atlas_audit_reader;
GRANT SELECT (debit, credit, amount)
    ON TABLE public.ledger_entries TO atlas_audit_reader;
GRANT SELECT (id, entry_type, idempotency_key, reference_type, reference_id)
    ON TABLE public.ledger_journals TO atlas_audit_reader;
GRANT SELECT (id, journal_id, debit, credit)
    ON TABLE public.ledger_journal_lines TO atlas_audit_reader;
GRANT SELECT (ledger_sequence, ledger_entry_sha256)
    ON TABLE public.research_trial_archive TO atlas_audit_reader;
GRANT SELECT (id, last_sequence, last_entry_sha256)
    ON TABLE public.research_trial_ledger_head TO atlas_audit_reader;
GRANT SELECT (scope_sha256, source_sample_sha256, report_sha256, holdout_version, holdout_plan_json)
    ON TABLE public.research_holdout_registry TO atlas_audit_reader;
GRANT SELECT (created_at, request_id, provider_id, provider, amount, currency, status, provider_status)
    ON TABLE public.withdrawals TO atlas_audit_reader;

ALTER ROLE atlas_audit_reader SET default_transaction_read_only = on;
ALTER ROLE atlas_audit_reader SET search_path = pg_catalog, public;
COMMIT;

-- Return capability metadata only; no credentials or customer data.
SELECT r.rolname, r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreaterole,
       r.rolcreatedb, r.rolreplication, r.rolbypassrls,
       has_schema_privilege(r.rolname,'public','USAGE') AS public_usage,
       has_schema_privilege(r.rolname,'public','CREATE') AS public_create,
       (SELECT count(*) FROM pg_auth_members m WHERE m.member=r.oid) AS memberships,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relowner=r.oid) AS owned_public_tables,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND c.relname NOT IN (
            'alembic_version','customer_ledger_accounts','ledger_entries','ledger_journals',
            'ledger_journal_lines','research_trial_archive','research_trial_ledger_head',
            'research_holdout_registry','withdrawals'
          ) AND has_table_privilege(r.rolname,c.oid,'SELECT')) AS unexpected_select,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p') AND (
          has_table_privilege(r.rolname,c.oid,'INSERT') OR has_table_privilege(r.rolname,c.oid,'UPDATE')
          OR has_table_privilege(r.rolname,c.oid,'DELETE') OR has_table_privilege(r.rolname,c.oid,'TRUNCATE')
          OR has_table_privilege(r.rolname,c.oid,'REFERENCES') OR has_table_privilege(r.rolname,c.oid,'TRIGGER')
        )) AS writable_public_tables,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='S' AND (
          has_sequence_privilege(r.rolname,c.oid,'USAGE') OR has_sequence_privilege(r.rolname,c.oid,'SELECT')
          OR has_sequence_privilege(r.rolname,c.oid,'UPDATE')
        )) AS accessible_public_sequences,
       (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND has_function_privilege(r.rolname,p.oid,'EXECUTE')) AS executable_public_functions
FROM pg_roles r WHERE r.rolname='atlas_audit_reader';