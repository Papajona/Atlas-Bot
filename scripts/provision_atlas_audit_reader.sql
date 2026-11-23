\set ON_ERROR_STOP on

-- Dedicated read-only identity for restore and provider-ledger audits.
-- Apply only after alembic is at 0060_webhook_events_created_at_index and after a
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
                   WHERE version_num = '0060_webhook_events_created_at_index') THEN
        RAISE EXCEPTION 'database must be at 0060_webhook_events_created_at_index before audit role provisioning';
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

-- The verifier checks effective function privileges, including grants inherited
-- from PUBLIC. Do not change unrelated function ACLs here: fail and roll back.
DO $atlas_audit_public_function_guard$
DECLARE
    executable_public_functions integer;
BEGIN
    SELECT count(*) INTO executable_public_functions
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public'
      AND has_function_privilege('atlas_audit_reader', p.oid, 'EXECUTE');
    IF executable_public_functions <> 0 THEN
        RAISE EXCEPTION
            'atlas_audit_reader has effective EXECUTE on % public functions; the function owner must revoke unintended PUBLIC grants or correct default privileges before retrying',
            executable_public_functions;
    END IF;
END
$atlas_audit_public_function_guard$;

ALTER ROLE atlas_audit_reader SET default_transaction_read_only = on;
ALTER ROLE atlas_audit_reader SET search_path = pg_catalog, public;
COMMIT;

-- Return capability metadata only; no credentials or customer data.
WITH allowed(table_name, column_name) AS (
    VALUES
      ('alembic_version','version_num'),
      ('customer_ledger_accounts','available'),
      ('customer_ledger_accounts','trading_reserved'),
      ('customer_ledger_accounts','withdrawal_reserved'),
      ('ledger_entries','debit'),('ledger_entries','credit'),('ledger_entries','amount'),
      ('ledger_journals','id'),('ledger_journals','entry_type'),
      ('ledger_journals','idempotency_key'),('ledger_journals','reference_type'),
      ('ledger_journals','reference_id'),
      ('ledger_journal_lines','id'),('ledger_journal_lines','journal_id'),
      ('ledger_journal_lines','debit'),('ledger_journal_lines','credit'),
      ('research_trial_archive','ledger_sequence'),('research_trial_archive','ledger_entry_sha256'),
      ('research_trial_ledger_head','id'),('research_trial_ledger_head','last_sequence'),
      ('research_trial_ledger_head','last_entry_sha256'),
      ('research_holdout_registry','scope_sha256'),('research_holdout_registry','source_sample_sha256'),
      ('research_holdout_registry','report_sha256'),('research_holdout_registry','holdout_version'),
      ('research_holdout_registry','holdout_plan_json'),
      ('withdrawals','created_at'),('withdrawals','request_id'),('withdrawals','provider_id'),
      ('withdrawals','provider'),('withdrawals','amount'),('withdrawals','currency'),
      ('withdrawals','status'),('withdrawals','provider_status')
),
observed AS (
    SELECT r.*,
      (SELECT count(*) FROM allowed a
       LEFT JOIN pg_class c ON c.relnamespace='public'::regnamespace
                           AND c.relname=a.table_name AND c.relkind IN ('r','p')
       LEFT JOIN pg_attribute att ON att.attrelid=c.oid AND att.attname=a.column_name
                                 AND att.attnum>0 AND NOT att.attisdropped
       WHERE att.attnum IS NULL
          OR NOT has_column_privilege(r.rolname,c.oid,att.attnum,'SELECT')
      ) AS missing_allowed_select_columns,
      (SELECT count(*) FROM pg_class c
       JOIN pg_namespace n ON n.oid=c.relnamespace
       JOIN pg_attribute att ON att.attrelid=c.oid
       WHERE n.nspname='public' AND c.relkind IN ('r','p')
         AND att.attnum>0 AND NOT att.attisdropped
         AND has_column_privilege(r.rolname,c.oid,att.attnum,'SELECT')
         AND NOT EXISTS (SELECT 1 FROM allowed a
                         WHERE a.table_name=c.relname AND a.column_name=att.attname)
      ) AS unexpected_select_columns,
      (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname='public' AND c.relkind IN ('r','p') AND (
         has_table_privilege(r.rolname,c.oid,'INSERT')
         OR has_table_privilege(r.rolname,c.oid,'UPDATE')
         OR has_table_privilege(r.rolname,c.oid,'DELETE')
         OR has_table_privilege(r.rolname,c.oid,'TRUNCATE')
         OR has_table_privilege(r.rolname,c.oid,'REFERENCES')
         OR has_table_privilege(r.rolname,c.oid,'TRIGGER')
         OR EXISTS (
           SELECT 1 FROM pg_attribute att
           WHERE att.attrelid=c.oid AND att.attnum>0 AND NOT att.attisdropped AND (
             has_column_privilege(r.rolname,c.oid,att.attnum,'INSERT')
             OR has_column_privilege(r.rolname,c.oid,att.attnum,'UPDATE')
             OR has_column_privilege(r.rolname,c.oid,att.attnum,'REFERENCES')
           )
         )
       )) AS writable_public_objects,
      (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname='public' AND c.relkind='S' AND (
         has_sequence_privilege(r.rolname,c.oid,'USAGE')
         OR has_sequence_privilege(r.rolname,c.oid,'SELECT')
         OR has_sequence_privilege(r.rolname,c.oid,'UPDATE')
       )) AS public_sequence_privileges,
      (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
       WHERE n.nspname='public' AND has_function_privilege(r.rolname,p.oid,'EXECUTE')
      ) AS public_function_execute_privileges,
      (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relowner=r.oid
      ) AS owned_public_tables,
      (SELECT count(*) FROM pg_auth_members m WHERE m.member=r.oid) AS memberships
    FROM pg_roles r
    WHERE r.rolname='atlas_audit_reader'
)
SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolcreatedb, rolreplication,
       rolbypassrls, has_schema_privilege(rolname,'public','USAGE') AS public_usage,
       has_schema_privilege(rolname,'public','CREATE') AS public_create,
       memberships, owned_public_tables, missing_allowed_select_columns,
       unexpected_select_columns, writable_public_objects, public_sequence_privileges,
       public_function_execute_privileges
FROM observed;