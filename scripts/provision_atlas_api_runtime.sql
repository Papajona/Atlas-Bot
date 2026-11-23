\set ON_ERROR_STOP on

-- Dedicated server-side production API login. Run only after verified backup/restore
-- and Alembic head 0064_strategy_outcome_recent_index. Set its password separately with psql's hidden \password
-- prompt; never store passwords in this file or place them in chat.
BEGIN;

DO $guard$
DECLARE
    missing_tables text[];
    unprotected_tables integer;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'atlas_api_runtime') THEN
        RAISE EXCEPTION 'atlas_api_runtime already exists; inspect it rather than reusing or widening it';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolcreaterole) THEN
        RAISE EXCEPTION 'current role cannot provision the production API login';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.alembic_version
                   WHERE version_num = '0064_strategy_outcome_recent_index') THEN
        RAISE EXCEPTION 'database must be at 0064_strategy_outcome_recent_index before API role provisioning';
    END IF;

    SELECT array_agg(required.table_name ORDER BY required.table_name)
      INTO missing_tables
    FROM unnest(ARRAY[
        'alembic_version','audit_log','audit_log_external_delivery',
        'customer_paper_submission_audit_retries','customer_ledger_accounts',
        'ledger_entries','ledger_journals','ledger_journal_lines',
        'research_trial_archive','research_trial_ledger_head',
        'research_holdout_registry','trades','withdrawals'
    ]::text[]) AS required(table_name)
    WHERE to_regclass(format('public.%I', required.table_name)) IS NULL;
    IF coalesce(array_length(missing_tables, 1), 0) > 0 THEN
        RAISE EXCEPTION 'required production tables are missing: %', missing_tables;
    END IF;

    SELECT count(*) INTO unprotected_tables
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT c.relrowsecurity;
    IF unprotected_tables <> 0 THEN
        RAISE EXCEPTION 'public RLS lockdown is incomplete; refusing to provision API identity';
    END IF;
END
$guard$;

CREATE ROLE atlas_api_runtime
    LOGIN NOINHERIT BYPASSRLS
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

GRANT CONNECT ON DATABASE postgres TO atlas_api_runtime;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM atlas_api_runtime;
GRANT USAGE ON SCHEMA public TO atlas_api_runtime;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM atlas_api_runtime;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM atlas_api_runtime;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM atlas_api_runtime;

-- Ordinary mutable application tables receive SELECT/INSERT/UPDATE only.
-- DELETE is withheld from financial, custody, execution, and audit history.
-- The sole exception is owner-scoped, TTL-governed customer benchmark capture data.
DO $atlas_api_runtime_grants$
DECLARE item record;
BEGIN
    FOR item IN
        SELECT c.relname
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND c.relname NOT IN (
            'alembic_version','audit_log','audit_log_external_delivery',
            'customer_paper_submission_audit_retries','customer_ledger_accounts',
            'withdrawals','trades','ledger_entries','ledger_journals','ledger_journal_lines',
            'research_trial_archive','research_trial_ledger_head','research_holdout_registry',
            'funding_transactions','custody_transfers','tron_sweeps',
            'revenue_ledger','cost_ledger','referral_commissions',
            'positions','order_commands','audit_chain_state',
            'customer_benchmark_captures'
          )
    LOOP
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO atlas_api_runtime',
            item.relname
        );
    END LOOP;
END
$atlas_api_runtime_grants$;

-- Explicit append-only / controlled-state grants.
-- Financial account and withdrawal history may be updated, never deleted.
GRANT SELECT ON TABLE public.alembic_version TO atlas_api_runtime;
GRANT SELECT, INSERT, UPDATE ON TABLE
    public.customer_ledger_accounts, public.withdrawals, public.trades
TO atlas_api_runtime;
GRANT SELECT, INSERT ON TABLE
    public.audit_log, public.ledger_entries, public.ledger_journals,
    public.ledger_journal_lines, public.research_trial_archive
TO atlas_api_runtime;
GRANT SELECT, INSERT, UPDATE ON TABLE
    public.audit_log_external_delivery, public.customer_paper_submission_audit_retries
TO atlas_api_runtime;
GRANT SELECT, UPDATE ON TABLE public.research_trial_ledger_head TO atlas_api_runtime;
GRANT SELECT, INSERT ON TABLE public.research_holdout_registry TO atlas_api_runtime;

-- State/history tables receive only the writes used by current application paths.
-- The application has no DELETE path for any of these retained records.
GRANT SELECT, INSERT, UPDATE ON TABLE
    public.funding_transactions, public.custody_transfers, public.tron_sweeps,
    public.positions, public.order_commands, public.audit_chain_state
TO atlas_api_runtime;
-- Billing ledgers are append-only; referral commissions are read-only in the app.
GRANT SELECT, INSERT ON TABLE public.revenue_ledger, public.cost_ledger TO atlas_api_runtime;
GRANT SELECT ON TABLE public.referral_commissions TO atlas_api_runtime;

-- Check effective privileges, including PUBLIC grants, before committing the login.
DO $atlas_api_runtime_privilege_guard$
DECLARE
    unexpected_delete_privileges integer;
    executable_public_functions integer;
BEGIN
    SELECT count(*) INTO unexpected_delete_privileges
    FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='public' AND c.relkind IN ('r','p')
      AND c.relname <> 'customer_benchmark_captures'
      AND has_table_privilege('atlas_api_runtime', c.oid, 'DELETE');
    IF unexpected_delete_privileges <> 0 THEN
        RAISE EXCEPTION
            'atlas_api_runtime has DELETE on % public tables other than benchmark captures; refusing to commit the production identity',
            unexpected_delete_privileges;
    END IF;

    SELECT count(*) INTO executable_public_functions
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public'
      AND has_function_privilege('atlas_api_runtime', p.oid, 'EXECUTE');
    IF executable_public_functions <> 0 THEN
        RAISE EXCEPTION
            'atlas_api_runtime has effective EXECUTE on % public functions; correct PUBLIC/default function grants before retrying',
            executable_public_functions;
    END IF;
END
$atlas_api_runtime_privilege_guard$;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO atlas_api_runtime;
GRANT SELECT, INSERT, DELETE
    ON TABLE public.customer_benchmark_captures TO atlas_api_runtime;
GRANT USAGE, SELECT
    ON SEQUENCE public.customer_benchmark_captures_id_seq TO atlas_api_runtime;

DO $atlas_benchmark_capture_update_guard$
BEGIN
    IF has_table_privilege(
        'atlas_api_runtime',
        'public.customer_benchmark_captures',
        'UPDATE'
    ) THEN
        RAISE EXCEPTION
            'atlas_api_runtime must not UPDATE benchmark consent, ciphertext, or retention fields';
    END IF;
END
$atlas_benchmark_capture_update_guard$;

ALTER ROLE atlas_api_runtime SET default_transaction_read_only = off;
ALTER ROLE atlas_api_runtime SET search_path = pg_catalog, public;
COMMIT;

-- Verify the runtime identity before its URL is stored in Secret Manager.
SELECT r.rolname, r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreaterole,
       r.rolcreatedb, r.rolreplication, r.rolbypassrls,
       has_schema_privilege(r.rolname,'public','USAGE') AS public_usage,
       has_schema_privilege(r.rolname,'public','CREATE') AS public_create,
       (SELECT count(*) FROM pg_auth_members m WHERE m.member=r.oid) AS memberships,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relowner=r.oid) AS owned_public_tables,
       has_table_privilege(r.rolname,'public.audit_log','INSERT') AS audit_insert,
       has_table_privilege(r.rolname,'public.trades','INSERT') AS trades_insert,
       has_table_privilege(r.rolname,'public.customer_ledger_accounts','UPDATE') AS ledger_update,
       has_table_privilege(r.rolname,'public.customer_ledger_accounts','DELETE') AS ledger_delete,
       has_table_privilege(r.rolname,'public.withdrawals','DELETE') AS withdrawal_delete,
       has_table_privilege(r.rolname,'public.trades','DELETE') AS trades_delete,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
          AND c.relname <> 'customer_benchmark_captures'
          AND has_table_privilege(r.rolname,c.oid,'DELETE')) AS public_tables_with_delete_privileges,
       has_table_privilege(r.rolname,'public.customer_benchmark_captures','DELETE') AS benchmark_capture_delete,
       has_table_privilege(r.rolname,'public.customer_benchmark_captures','UPDATE') AS benchmark_capture_update,
       has_table_privilege(r.rolname,'public.research_trial_ledger_head','UPDATE') AS trial_head_update,
       has_table_privilege(r.rolname,'public.research_holdout_registry','INSERT') AS holdout_insert,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p') AND (
          has_table_privilege(r.rolname,c.oid,'TRUNCATE')
          OR has_table_privilege(r.rolname,c.oid,'REFERENCES')
          OR has_table_privilege(r.rolname,c.oid,'TRIGGER')
        )) AS elevated_table_privileges,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='S'
          AND has_sequence_privilege(r.rolname,c.oid,'UPDATE')) AS sequence_setval_privileges,
       (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND has_function_privilege(r.rolname,p.oid,'EXECUTE')) AS executable_public_functions
FROM pg_roles r WHERE r.rolname='atlas_api_runtime';