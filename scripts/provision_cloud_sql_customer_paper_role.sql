\set ON_ERROR_STOP on

-- Create the non-privileged customer-paper login on an isolated Cloud SQL
-- recovery database. Run this only after reviewing the target and confirming
-- that the connected operator is allowed to create roles. Set its password
-- interactively with: \password atlas_customer_paper
BEGIN;

DO $atlas_cloud_sql_paper_role_guard$
DECLARE
    operator_can_manage_roles boolean;
    current_read_only boolean;
BEGIN
    SELECT (rolsuper OR rolcreaterole)
      INTO operator_can_manage_roles
      FROM pg_roles
     WHERE rolname = current_user;

    current_read_only := current_setting('transaction_read_only')::boolean;
    IF current_read_only THEN
        RAISE EXCEPTION 'refusing paper-role provisioning through a read-only connection';
    END IF;
    IF NOT COALESCE(operator_can_manage_roles, false) THEN
        RAISE EXCEPTION 'current role cannot create the customer-paper login';
    END IF;
    IF current_user IN (
        'atlas_api_runtime', 'atlas_audit_reader', 'atlas_customer_paper',
        'atlas_paper_reader', 'anon', 'authenticated', 'service_role'
    ) THEN
        RAISE EXCEPTION 'current role is not an approved database provisioning identity';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'atlas_customer_paper') THEN
        RAISE EXCEPTION 'atlas_customer_paper already exists; inspect its attributes and grants instead of replacing it';
    END IF;
END
$atlas_cloud_sql_paper_role_guard$;

CREATE ROLE atlas_customer_paper
    LOGIN NOINHERIT NOBYPASSRLS
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

DO $atlas_cloud_sql_paper_connect$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO atlas_customer_paper',
        current_database()
    );
END
$atlas_cloud_sql_paper_connect$;

ALTER ROLE atlas_customer_paper SET default_transaction_read_only = off;
ALTER ROLE atlas_customer_paper SET search_path = 'pg_catalog, customer_paper';

COMMIT;

SELECT r.rolname, r.rolcanlogin, r.rolinherit, r.rolsuper,
       r.rolcreaterole, r.rolcreatedb, r.rolreplication, r.rolbypassrls,
       has_database_privilege(r.rolname, current_database(), 'CONNECT') AS database_connect,
       to_regnamespace('customer_paper') IS NOT NULL AS paper_schema_present
FROM pg_roles r
WHERE r.rolname = 'atlas_customer_paper';
