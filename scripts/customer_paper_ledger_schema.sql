\set ON_ERROR_STOP on
\if :{?paper_runtime_role}
\else
  \echo "ERROR: pass -v paper_runtime_role=the_existing_dedicated_runtime_role"
  \quit 2
\endif

-- Run once as a reviewed database owner/admin on the designated paper database.
-- The role is provisioned separately and must be LOGIN, NOINHERIT, NOBYPASSRLS,
-- non-superuser, without CREATE ROLE/DB/replication, and without role memberships.
SELECT set_config('app.paper_runtime_role', :'paper_runtime_role', false);

DO $paper_role_check$
DECLARE role_name text := current_setting('app.paper_runtime_role', true);
BEGIN
    IF role_name IS NULL OR role_name !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'invalid paper runtime role name';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles r
        WHERE r.rolname = role_name AND r.rolcanlogin AND NOT r.rolsuper
          AND NOT r.rolcreaterole AND NOT r.rolcreatedb AND NOT r.rolreplication
          AND NOT r.rolinherit AND NOT r.rolbypassrls
          AND NOT EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid)
    ) THEN
        RAISE EXCEPTION 'paper runtime role does not have the required least-privilege attributes';
    END IF;
END
$paper_role_check$;

CREATE SCHEMA IF NOT EXISTS customer_paper AUTHORIZATION CURRENT_USER;
REVOKE ALL ON SCHEMA customer_paper FROM PUBLIC;

CREATE TABLE IF NOT EXISTS customer_paper.accounts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL CHECK (customer_id > 0),
    currency varchar(12) NOT NULL CHECK (currency = upper(currency)),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (customer_id, currency),
    UNIQUE (id, customer_id, currency)
);

CREATE TABLE IF NOT EXISTS customer_paper.journals (
    id uuid PRIMARY KEY,
    customer_id bigint NOT NULL CHECK (customer_id > 0),
    idempotency_key varchar(180) NOT NULL,
    event_type varchar(40) NOT NULL CHECK (
        event_type IN ('PAPER_SEED', 'SIMULATED_FILL', 'SIMULATED_FEE',
                       'SIMULATED_FUNDING', 'SIMULATED_REALIZED_PNL', 'RESET')
    ),
    reference varchar(180) NOT NULL DEFAULT '',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (customer_id, idempotency_key),
    UNIQUE (id, customer_id)
);

CREATE TABLE IF NOT EXISTS customer_paper.journal_lines (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    journal_id uuid NOT NULL,
    account_id bigint NOT NULL,
    customer_id bigint NOT NULL CHECK (customer_id > 0),
    currency varchar(12) NOT NULL CHECK (currency = upper(currency)),
    signed_amount numeric(38,18) NOT NULL CHECK (signed_amount <> 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (journal_id, customer_id)
        REFERENCES customer_paper.journals (id, customer_id),
    FOREIGN KEY (account_id, customer_id, currency)
        REFERENCES customer_paper.accounts (id, customer_id, currency),
    UNIQUE (journal_id, account_id)
);

CREATE OR REPLACE FUNCTION customer_paper.reject_mutation()
RETURNS trigger LANGUAGE plpgsql AS $paper_immutable$
BEGIN
    RAISE EXCEPTION 'customer paper ledger rows are append-only; post a compensating journal instead'
        USING ERRCODE = '55000';
END
$paper_immutable$;

DROP TRIGGER IF EXISTS trg_customer_paper_accounts_immutable ON customer_paper.accounts;
CREATE TRIGGER trg_customer_paper_accounts_immutable
BEFORE UPDATE OR DELETE ON customer_paper.accounts
FOR EACH ROW EXECUTE FUNCTION customer_paper.reject_mutation();
DROP TRIGGER IF EXISTS trg_customer_paper_journals_immutable ON customer_paper.journals;
CREATE TRIGGER trg_customer_paper_journals_immutable
BEFORE UPDATE OR DELETE ON customer_paper.journals
FOR EACH ROW EXECUTE FUNCTION customer_paper.reject_mutation();
DROP TRIGGER IF EXISTS trg_customer_paper_lines_immutable ON customer_paper.journal_lines;
CREATE TRIGGER trg_customer_paper_lines_immutable
BEFORE UPDATE OR DELETE ON customer_paper.journal_lines
FOR EACH ROW EXECUTE FUNCTION customer_paper.reject_mutation();

CREATE OR REPLACE FUNCTION customer_paper.assert_balanced_journal()
RETURNS trigger LANGUAGE plpgsql AS $paper_balance$
DECLARE
    target_id uuid;
    line_count bigint;
    currency_count bigint;
    total_amount numeric(38,18);
BEGIN
    IF TG_TABLE_NAME = 'journals' THEN
        target_id := NEW.id;
    ELSE
        target_id := NEW.journal_id;
    END IF;
    SELECT count(*), count(DISTINCT currency), coalesce(sum(signed_amount), 0)
      INTO line_count, currency_count, total_amount
      FROM customer_paper.journal_lines
     WHERE journal_id = target_id;
    IF line_count < 2 OR currency_count <> 1 OR total_amount <> 0 THEN
        RAISE EXCEPTION 'customer paper journal must contain at least two same-currency lines summing exactly to zero'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$paper_balance$;

DROP TRIGGER IF EXISTS trg_customer_paper_balance_journal ON customer_paper.journals;
CREATE CONSTRAINT TRIGGER trg_customer_paper_balance_journal
AFTER INSERT ON customer_paper.journals
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION customer_paper.assert_balanced_journal();
DROP TRIGGER IF EXISTS trg_customer_paper_balance_lines ON customer_paper.journal_lines;
CREATE CONSTRAINT TRIGGER trg_customer_paper_balance_lines
AFTER INSERT ON customer_paper.journal_lines
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION customer_paper.assert_balanced_journal();

ALTER TABLE customer_paper.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.accounts FORCE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.journals ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.journals FORCE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.journal_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.journal_lines FORCE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA customer_paper FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA customer_paper FROM PUBLIC;
DO $paper_grants$
DECLARE role_name text := current_setting('app.paper_runtime_role', true);
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
        EXECUTE 'REVOKE ALL ON SCHEMA customer_paper FROM anon';
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA customer_paper FROM anon';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA customer_paper FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
        EXECUTE 'REVOKE ALL ON SCHEMA customer_paper FROM authenticated';
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA customer_paper FROM authenticated';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA customer_paper FROM authenticated';
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA customer_paper TO %I', role_name);
    EXECUTE format('GRANT SELECT, INSERT ON customer_paper.accounts TO %I', role_name);
    EXECUTE format('GRANT SELECT, INSERT ON customer_paper.journals TO %I', role_name);
    EXECUTE format('GRANT SELECT, INSERT ON customer_paper.journal_lines TO %I', role_name);
    EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA customer_paper TO %I', role_name);
    FOREACH role_name IN ARRAY ARRAY['accounts', 'journals', 'journal_lines'] LOOP
        EXECUTE format('DROP POLICY IF EXISTS customer_paper_tenant ON customer_paper.%I', role_name);
        EXECUTE format(
            'CREATE POLICY customer_paper_tenant ON customer_paper.%I FOR ALL TO %I USING (customer_id = NULLIF(current_setting(''app.customer_id'', true), '''')::bigint) WITH CHECK (customer_id = NULLIF(current_setting(''app.customer_id'', true), '''')::bigint)',
            role_name, current_setting('app.paper_runtime_role', true)
        );
    END LOOP;
END
$paper_grants$;
