\set ON_ERROR_STOP on

-- Operator-only provisioning for the dedicated paper-staging API.
-- Run only after confirming a recoverable backup/export. Never execute against an
-- unreviewed project or reuse an existing role. No production-data writes are granted.
BEGIN;

DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'atlas_paper_reader') THEN
        RAISE EXCEPTION 'atlas_paper_reader already exists; inspect its attributes/grants instead of reusing it';
    END IF;
    IF to_regclass('public.customer_profiles') IS NULL
       OR to_regclass('public.app_state') IS NULL THEN
        RAISE EXCEPTION 'required public.customer_profiles or public.app_state table is missing';
    END IF;
END
$guard$;

CREATE ROLE atlas_paper_reader
    LOGIN NOINHERIT BYPASSRLS
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

GRANT CONNECT ON DATABASE postgres TO atlas_paper_reader;

-- Fail closed: this role has no object access except SELECT on these two existing
-- records/tables. BYPASSRLS is necessary because this project's locked-down public
-- tables have no tenant policies; explicit grants still constrain what can be read.
REVOKE ALL PRIVILEGES ON SCHEMA public FROM atlas_paper_reader;
GRANT USAGE ON SCHEMA public TO atlas_paper_reader;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM atlas_paper_reader;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM atlas_paper_reader;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM atlas_paper_reader;

GRANT SELECT ON TABLE public.customer_profiles, public.app_state
    TO atlas_paper_reader;

-- Defense in depth; table grants remain the authoritative write barrier.
ALTER ROLE atlas_paper_reader SET default_transaction_read_only = on;

COMMIT;

SELECT r.rolname, r.rolcanlogin, r.rolsuper, r.rolcreaterole, r.rolcreatedb,
       r.rolreplication, r.rolbypassrls, r.rolinherit,
       has_schema_privilege(r.rolname, 'public', 'USAGE') AS public_usage,
       has_schema_privilege(r.rolname, 'public', 'CREATE') AS public_create,
       has_table_privilege(r.rolname, 'public.customer_profiles', 'SELECT') AS read_profiles,
       has_table_privilege(r.rolname, 'public.app_state', 'SELECT') AS read_app_state,
       has_table_privilege(r.rolname, 'public.customer_profiles', 'INSERT') AS write_profiles,
       has_table_privilege(r.rolname, 'public.app_state', 'UPDATE') AS write_app_state
FROM pg_roles r
WHERE r.rolname = 'atlas_paper_reader';
