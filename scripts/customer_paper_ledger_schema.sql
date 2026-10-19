\set ON_ERROR_STOP on
\if :{?paper_runtime_role}
\else
  \echo "ERROR: pass -v paper_runtime_role=the_existing_dedicated_runtime_role"
  \quit 2
\endif

-- Execute only against the dedicated customer-paper PostgreSQL database, as a
-- reviewed database owner. Provision paper_runtime_role separately as LOGIN,
-- NOINHERIT, NOBYPASSRLS, non-superuser, with no memberships or admin privileges.
SELECT set_config('app.paper_runtime_role', :'paper_runtime_role', false);

DO $paper_role_check$
DECLARE role_name text := current_setting('app.paper_runtime_role', true);
BEGIN
    IF role_name IS NULL OR role_name !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
        RAISE EXCEPTION 'invalid paper runtime role name';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles r
        WHERE r.rolname = role_name AND r.rolcanlogin
          AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb
          AND NOT r.rolreplication AND NOT r.rolinherit AND NOT r.rolbypassrls
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
    currency varchar(12) NOT NULL CHECK (currency = upper(currency) AND currency ~ '^[A-Z0-9]{2,12}$'),
    account_code varchar(40) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_customer_paper_account_code CHECK (
        account_code = upper(account_code) AND account_code IN
        ('PAPER_CASH', 'PAPER_EQUITY', 'PAPER_POSITION', 'PAPER_FEES', 'PAPER_PNL', 'PAPER_CLEARING')
    ),
    UNIQUE (customer_id, currency, account_code),
    UNIQUE (id, customer_id, currency)
);

-- Upgrade the original three-table ledger layout if this script is run again
-- over a previously provisioned disposable paper database.
ALTER TABLE customer_paper.accounts
    ADD COLUMN IF NOT EXISTS account_code varchar(40) NOT NULL DEFAULT 'PAPER_CASH';
ALTER TABLE customer_paper.accounts DROP CONSTRAINT IF EXISTS accounts_customer_id_currency_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_paper_account_code
    ON customer_paper.accounts (customer_id, currency, account_code);
DO $paper_account_code_check$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='ck_customer_paper_account_code'
          AND conrelid='customer_paper.accounts'::regclass
    ) THEN
        ALTER TABLE customer_paper.accounts
            ADD CONSTRAINT ck_customer_paper_account_code CHECK (
                account_code = upper(account_code) AND account_code IN
                ('PAPER_CASH', 'PAPER_EQUITY', 'PAPER_POSITION', 'PAPER_FEES', 'PAPER_PNL', 'PAPER_CLEARING')
            );
    END IF;
END
$paper_account_code_check$;

CREATE TABLE IF NOT EXISTS customer_paper.journals (
    id uuid PRIMARY KEY,
    customer_id bigint NOT NULL CHECK (customer_id > 0),
    idempotency_key varchar(180) NOT NULL,
    event_type varchar(40) NOT NULL,
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
    currency varchar(12) NOT NULL CHECK (currency = upper(currency) AND currency ~ '^[A-Z0-9]{2,12}$'),
    signed_amount numeric(38,18) NOT NULL CHECK (signed_amount <> 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (journal_id, customer_id)
        REFERENCES customer_paper.journals (id, customer_id),
    FOREIGN KEY (account_id, customer_id, currency)
        REFERENCES customer_paper.accounts (id, customer_id, currency),
    UNIQUE (journal_id, account_id)
);

-- Idempotently replace the original journal event check with the full known set.
ALTER TABLE customer_paper.journals DROP CONSTRAINT IF EXISTS journals_event_type_check;
ALTER TABLE customer_paper.journals DROP CONSTRAINT IF EXISTS ck_customer_paper_journal_event_type;
ALTER TABLE customer_paper.journals
    ADD CONSTRAINT ck_customer_paper_journal_event_type
    CHECK (event_type IN ('PAPER_SEED', 'SIMULATED_FILL', 'SIMULATED_FEE',
        'SIMULATED_FUNDING', 'SIMULATED_REALIZED_PNL', 'REVERSAL', 'RESET'));

CREATE TABLE IF NOT EXISTS customer_paper.orders (
    id uuid PRIMARY KEY,
    customer_id bigint NOT NULL CHECK (customer_id > 0),
    client_order_id varchar(180) NOT NULL,
    venue varchar(40) NOT NULL CHECK (venue = lower(venue) AND venue ~ '^[a-z0-9_-]{1,40}$'),
    market_type varchar(16) NOT NULL CHECK (market_type IN ('spot','swap','forex','commodity')),
    symbol varchar(80) NOT NULL CHECK (symbol = upper(symbol) AND symbol ~ '^[A-Z0-9][A-Z0-9/_:.-]{0,79}$'),
    side varchar(8) NOT NULL CHECK (side IN ('BUY','SELL')),
    order_type varchar(12) NOT NULL CHECK (order_type IN ('MARKET','LIMIT')),
    quantity numeric(38,18) NOT NULL CHECK (quantity > 0),
    limit_price numeric(38,18),
    currency varchar(12) NOT NULL CHECK (currency = upper(currency) AND currency ~ '^[A-Z0-9]{2,12}$'),
    payload_sha256 varchar(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((order_type='LIMIT' AND limit_price IS NOT NULL AND limit_price > 0)
        OR (order_type='MARKET' AND limit_price IS NULL)),
    UNIQUE (customer_id, client_order_id),
    UNIQUE (id, customer_id)
);

CREATE TABLE IF NOT EXISTS customer_paper.fills (
    id uuid PRIMARY KEY,
    customer_id bigint NOT NULL CHECK (customer_id > 0),
    order_id uuid NOT NULL,
    idempotency_key varchar(150) NOT NULL,
    bar_key varchar(180) NOT NULL,
    venue varchar(40) NOT NULL CHECK (venue = lower(venue) AND venue ~ '^[a-z0-9_-]{1,40}$'),
    market_type varchar(16) NOT NULL CHECK (market_type IN ('spot','swap','forex','commodity')),
    symbol varchar(80) NOT NULL CHECK (symbol = upper(symbol) AND symbol ~ '^[A-Z0-9][A-Z0-9/_:.-]{0,79}$'),
    side varchar(8) NOT NULL CHECK (side IN ('BUY','SELL')),
    quantity numeric(38,18) NOT NULL CHECK (quantity > 0),
    price numeric(38,18) NOT NULL CHECK (price > 0),
    fee_amount numeric(38,18) NOT NULL DEFAULT 0 CHECK (fee_amount >= 0),
    currency varchar(12) NOT NULL CHECK (currency = upper(currency) AND currency ~ '^[A-Z0-9]{2,12}$'),
    realized_pnl numeric(38,18) NOT NULL DEFAULT 0,
    cash_delta numeric(38,18) NOT NULL,
    position_delta numeric(38,18) NOT NULL,
    pnl_delta numeric(38,18) NOT NULL DEFAULT 0,
    quantity_before numeric(38,18) NOT NULL DEFAULT 0 CHECK (quantity_before >= 0),
    average_entry_before numeric(38,18) NOT NULL DEFAULT 0 CHECK (average_entry_before >= 0),
    cost_basis_before numeric(38,18) NOT NULL DEFAULT 0 CHECK (cost_basis_before >= 0),
    quantity_after numeric(38,18) NOT NULL CHECK (quantity_after >= 0),
    average_entry_after numeric(38,18) NOT NULL CHECK (average_entry_after >= 0),
    cost_basis_after numeric(38,18) NOT NULL CHECK (cost_basis_after >= 0),
    realized_pnl_before numeric(38,18) NOT NULL DEFAULT 0,
    realized_pnl_after numeric(38,18) NOT NULL,
    payload_sha256 varchar(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (order_id, customer_id) REFERENCES customer_paper.orders(id, customer_id),
    UNIQUE (customer_id, idempotency_key),
    UNIQUE (customer_id, order_id, bar_key),
    UNIQUE (id, customer_id)
);

CREATE TABLE IF NOT EXISTS customer_paper.order_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL CHECK (customer_id > 0),
    order_id uuid NOT NULL,
    event_key varchar(180) NOT NULL,
    event_type varchar(24) NOT NULL CHECK (
        event_type IN ('SUBMITTED','PARTIALLY_FILLED','FILLED','CANCELLED','EXPIRED','REJECTED')
    ),
    filled_quantity numeric(38,18) NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (order_id, customer_id) REFERENCES customer_paper.orders(id, customer_id),
    UNIQUE (customer_id, event_key)
);

CREATE TABLE IF NOT EXISTS customer_paper.position_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL CHECK (customer_id > 0),
    venue varchar(40) NOT NULL CHECK (venue = lower(venue) AND venue ~ '^[a-z0-9_-]{1,40}$'),
    market_type varchar(16) NOT NULL CHECK (market_type IN ('spot','swap','forex','commodity')),
    symbol varchar(80) NOT NULL CHECK (symbol = upper(symbol) AND symbol ~ '^[A-Z0-9][A-Z0-9/_:.-]{0,79}$'),
    fill_id uuid NOT NULL,
    reversal_of_fill_id uuid,
    event_type varchar(12) NOT NULL CHECK (event_type IN ('FILL','REVERSAL')),
    idempotency_key varchar(180) NOT NULL,
    signed_quantity numeric(38,18) NOT NULL CHECK (signed_quantity <> 0),
    fill_price numeric(38,18) NOT NULL CHECK (fill_price > 0),
    quantity_before numeric(38,18) NOT NULL DEFAULT 0 CHECK (quantity_before >= 0),
    average_entry_before numeric(38,18) NOT NULL DEFAULT 0 CHECK (average_entry_before >= 0),
    cost_basis_before numeric(38,18) NOT NULL DEFAULT 0 CHECK (cost_basis_before >= 0),
    quantity_after numeric(38,18) NOT NULL CHECK (quantity_after >= 0),
    average_entry_after numeric(38,18) NOT NULL CHECK (average_entry_after >= 0),
    cost_basis_after numeric(38,18) NOT NULL CHECK (cost_basis_after >= 0),
    realized_pnl_delta numeric(38,18) NOT NULL DEFAULT 0,
    realized_pnl_after numeric(38,18) NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (fill_id, customer_id) REFERENCES customer_paper.fills(id, customer_id),
    FOREIGN KEY (reversal_of_fill_id, customer_id) REFERENCES customer_paper.fills(id, customer_id),
    CHECK ((event_type='FILL' AND reversal_of_fill_id IS NULL)
        OR (event_type='REVERSAL' AND reversal_of_fill_id IS NOT NULL)),
    UNIQUE (customer_id, idempotency_key),
    UNIQUE (customer_id, reversal_of_fill_id)
);

CREATE OR REPLACE FUNCTION customer_paper.reject_mutation()
RETURNS trigger LANGUAGE plpgsql AS $paper_immutable$
BEGIN
    RAISE EXCEPTION 'customer paper ledger rows are append-only; post a compensating journal instead'
        USING ERRCODE = '55000';
END
$paper_immutable$;

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

CREATE OR REPLACE FUNCTION customer_paper.assert_order_event_state()
RETURNS trigger LANGUAGE plpgsql AS $paper_event_state$
DECLARE
    previous_event text;
    previous_filled numeric(38,18);
    maximum_quantity numeric(38,18);
BEGIN
    SELECT event_type, filled_quantity INTO previous_event, previous_filled
    FROM customer_paper.order_events
    WHERE customer_id = NEW.customer_id AND order_id = NEW.order_id
      AND id <> NEW.id
    ORDER BY id DESC LIMIT 1;
    SELECT quantity INTO maximum_quantity
    FROM customer_paper.orders WHERE id = NEW.order_id AND customer_id = NEW.customer_id;

    IF NEW.event_type = 'SUBMITTED' THEN
        IF previous_event IS NOT NULL OR NEW.filled_quantity <> 0 THEN
            RAISE EXCEPTION 'SUBMITTED must be the first order event with zero fill quantity'
                USING ERRCODE = '23514';
        END IF;
    ELSIF previous_event IS NULL OR previous_event NOT IN ('SUBMITTED','PARTIALLY_FILLED') THEN
        RAISE EXCEPTION 'order terminal state cannot be changed'
            USING ERRCODE = '23514';
    ELSIF NEW.filled_quantity < previous_filled OR NEW.filled_quantity > maximum_quantity THEN
        RAISE EXCEPTION 'order filled quantity must be monotonic and bounded by order quantity'
            USING ERRCODE = '23514';
    ELSIF NEW.event_type IN ('PARTIALLY_FILLED','FILLED') AND NEW.filled_quantity <= 0 THEN
        RAISE EXCEPTION 'filled event requires positive cumulative quantity'
            USING ERRCODE = '23514';
    ELSIF NEW.event_type = 'PARTIALLY_FILLED' AND NEW.filled_quantity >= maximum_quantity THEN
        RAISE EXCEPTION 'PARTIALLY_FILLED must leave a positive quantity open'
            USING ERRCODE = '23514';
    ELSIF NEW.event_type = 'FILLED' AND NEW.filled_quantity <> maximum_quantity THEN
        RAISE EXCEPTION 'FILLED requires the full ordered quantity'
            USING ERRCODE = '23514';
    ELSIF NEW.event_type IN ('CANCELLED','EXPIRED','REJECTED') AND NEW.filled_quantity <> previous_filled THEN
        RAISE EXCEPTION 'terminal order event cannot change cumulative fills'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$paper_event_state$;

-- Deferrable because SUBMITTED and its corresponding order row are inserted in one transaction.
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

DROP TRIGGER IF EXISTS trg_customer_paper_order_state_guard ON customer_paper.order_events;
CREATE CONSTRAINT TRIGGER trg_customer_paper_order_state_guard
AFTER INSERT ON customer_paper.order_events
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION customer_paper.assert_order_event_state();

DO $paper_immutable_triggers$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'accounts','journals','journal_lines','orders','order_events','fills','position_events'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_customer_paper_%I_immutable ON customer_paper.%I', table_name, table_name);
        EXECUTE format('CREATE TRIGGER trg_customer_paper_%I_immutable BEFORE UPDATE OR DELETE ON customer_paper.%I FOR EACH ROW EXECUTE FUNCTION customer_paper.reject_mutation()', table_name, table_name);
    END LOOP;
END
$paper_immutable_triggers$;

ALTER TABLE customer_paper.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.accounts FORCE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.journals ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.journals FORCE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.journal_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.journal_lines FORCE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.orders FORCE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.order_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.order_events FORCE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.fills ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.fills FORCE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.position_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_paper.position_events FORCE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA customer_paper FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA customer_paper FROM PUBLIC;
DO $paper_grants$
DECLARE
    role_name text := current_setting('app.paper_runtime_role', true);
    policy_table text;
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
    EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', role_name);
    EXECUTE format('GRANT USAGE ON SCHEMA customer_paper TO %I', role_name);
    FOREACH policy_table IN ARRAY ARRAY[
        'accounts','journals','journal_lines','orders','order_events','fills','position_events'
    ] LOOP
        EXECUTE format('GRANT SELECT, INSERT ON customer_paper.%I TO %I', policy_table, role_name);
        EXECUTE format('DROP POLICY IF EXISTS customer_paper_tenant ON customer_paper.%I', policy_table);
        EXECUTE format(
            'CREATE POLICY customer_paper_tenant ON customer_paper.%I FOR ALL TO %I USING (customer_id = NULLIF(current_setting(''app.customer_id'', true), '''')::bigint) WITH CHECK (customer_id = NULLIF(current_setting(''app.customer_id'', true), '''')::bigint)',
            policy_table, role_name
        );
    END LOOP;
    EXECUTE format('GRANT USAGE ON ALL SEQUENCES IN SCHEMA customer_paper TO %I', role_name);
END
$paper_grants$;
