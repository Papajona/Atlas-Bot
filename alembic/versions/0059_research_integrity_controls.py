"""Durable prospective trial chain and one-time holdout registry.

Revision ID: 0059_research_integrity_controls
Revises: 0058_customer_paper_rls_and_index_hardening
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0059_research_integrity_controls"
down_revision = "0058_customer_paper_rls_and_index_hardening"
branch_labels = None
depends_on = None


def _secure_service_tables(bind) -> None:
    if bind.dialect.name == "sqlite":
        # SQLite is used for local/test workflows. Protect one-time consumption
        # against row mutation there too; SQLite has no TRUNCATE statement/event.
        op.execute(sa.text("""
            CREATE TRIGGER IF NOT EXISTS trg_research_holdout_registry_no_update
            BEFORE UPDATE ON research_holdout_registry
            BEGIN
                SELECT RAISE(ABORT, 'research_holdout_registry is write-once');
            END;
        """))
        op.execute(sa.text("""
            CREATE TRIGGER IF NOT EXISTS trg_research_holdout_registry_no_delete
            BEFORE DELETE ON research_holdout_registry
            BEGIN
                SELECT RAISE(ABORT, 'research_holdout_registry is write-once');
            END;
        """))
        return
    if bind.dialect.name != "postgresql":
        return
    for table in ("research_trial_ledger_head", "research_holdout_registry"):
        op.execute(sa.text(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"REVOKE ALL PRIVILEGES ON TABLE public.{table} FROM PUBLIC"))
    op.execute(sa.text("""
        DO $atlas_research_integrity_public_acl$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.research_trial_ledger_head FROM anon';
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.research_holdout_registry FROM anon';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.research_trial_ledger_head FROM authenticated';
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.research_holdout_registry FROM authenticated';
            END IF;
        END
        $atlas_research_integrity_public_acl$;
    """))
    op.execute(sa.text("""
        DO $atlas_research_integrity_acl$
        DECLARE role_name text;
        BEGIN
            FOR role_name IN
                SELECT r.rolname
                FROM pg_roles r
                WHERE r.rolcanlogin AND r.rolbypassrls
                  AND NOT r.rolsuper AND NOT r.rolcreaterole AND NOT r.rolcreatedb
                  AND NOT r.rolreplication AND NOT r.rolinherit
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid
                  )
                  AND r.rolname NOT IN ('anon', 'authenticated', 'atlas_paper_reader', 'atlas_audit_reader')
            LOOP
                EXECUTE format(
                    'GRANT SELECT, UPDATE ON TABLE public.research_trial_ledger_head TO %I',
                    role_name
                );
                EXECUTE format(
                    'GRANT SELECT, INSERT ON TABLE public.research_holdout_registry TO %I',
                    role_name
                );
            END LOOP;
            -- The staging primary identity must not read or write research tables.
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'atlas_paper_reader') THEN
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.research_trial_ledger_head FROM atlas_paper_reader';
                EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.research_holdout_registry FROM atlas_paper_reader';
            END IF;
        END
        $atlas_research_integrity_acl$;
    """))
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION public.atlas_research_holdout_registry_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $atlas_holdout_immutable$
        BEGIN
            RAISE EXCEPTION 'research_holdout_registry is write-once; UPDATE/DELETE is prohibited'
                USING ERRCODE = '55000';
        END
        $atlas_holdout_immutable$;
    """))
    # asyncpg executes prepared statements and rejects multiple top-level SQL
    # commands in one statement. Keep every DROP/CREATE as its own migration call.
    op.execute(sa.text("""
        DROP TRIGGER IF EXISTS trg_research_holdout_registry_immutable
            ON public.research_holdout_registry
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_research_holdout_registry_immutable
        BEFORE UPDATE OR DELETE ON public.research_holdout_registry
        FOR EACH ROW EXECUTE FUNCTION public.atlas_research_holdout_registry_immutable()
    """))
    op.execute(sa.text("""
        DROP TRIGGER IF EXISTS trg_research_holdout_registry_no_truncate
            ON public.research_holdout_registry
    """))
    op.execute(sa.text("""
        CREATE TRIGGER trg_research_holdout_registry_no_truncate
        BEFORE TRUNCATE ON public.research_holdout_registry
        FOR EACH STATEMENT EXECUTE FUNCTION public.atlas_research_holdout_registry_immutable()
    """))



def _secure_ledger_journal_trigger_owner(bind) -> None:
    """Run deferred ledger validation under a non-login, read-only function owner.

    The production and audit identities deliberately receive no EXECUTE grants
    on public functions. The deferred trigger still needs to call the validation
    helper, so its SECURITY DEFINER context is isolated in a NOLOGIN role with
    SELECT-only ledger access and no role memberships.
    """
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("""
        DO $atlas_ledger_trigger_owner$
        DECLARE
            role_name constant text := 'atlas_ledger_trigger_owner';
            migration_role text := current_user;
            role_oid oid;
            trigger_function_owner oid;
            function_signature text;
            grantee_name text;
        BEGIN
            IF to_regprocedure('public.atlas_ledger_journal_deferred_check()') IS NULL
               OR to_regprocedure('public.atlas_validate_ledger_journal(integer)') IS NULL THEN
                RAISE EXCEPTION
                    'ledger journal trigger and validation functions must exist before trigger-owner hardening';
            END IF;

            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                EXECUTE
                    'CREATE ROLE atlas_ledger_trigger_owner '
                    'NOLOGIN NOINHERIT BYPASSRLS NOSUPERUSER '
                    'NOCREATEDB NOCREATEROLE NOREPLICATION';
            END IF;

            SELECT oid INTO role_oid
            FROM pg_roles
            WHERE rolname = role_name;

            IF NOT EXISTS (
                SELECT 1
                FROM pg_roles
                WHERE oid = role_oid
                  AND NOT rolcanlogin
                  AND NOT rolinherit
                  AND rolbypassrls
                  AND NOT rolsuper
                  AND NOT rolcreatedb
                  AND NOT rolcreaterole
                  AND NOT rolreplication
            ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner has unsafe role attributes; inspect it rather than widening or reusing it';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_auth_members
                WHERE roleid = role_oid OR member = role_oid
            ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner must not have or be granted any role memberships';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_class
                WHERE relowner = role_oid
            ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner must not own tables, sequences, or other relations';
            END IF;

            EXECUTE 'GRANT USAGE ON SCHEMA public TO atlas_ledger_trigger_owner';
            EXECUTE
                'GRANT SELECT ON TABLE public.ledger_journals, public.ledger_journal_lines '
                'TO atlas_ledger_trigger_owner';
            EXECUTE
                'GRANT EXECUTE ON FUNCTION public.atlas_validate_ledger_journal(integer) '
                'TO atlas_ledger_trigger_owner';

            SELECT proowner INTO trigger_function_owner
            FROM pg_proc
            WHERE oid = to_regprocedure('public.atlas_ledger_journal_deferred_check()');

            IF trigger_function_owner <> role_oid THEN
                IF migration_role = role_name THEN
                    RAISE EXCEPTION
                        'migration must use a separate administrative identity to assign the ledger trigger owner';
                END IF;

                -- ALTER FUNCTION OWNER requires CREATE on its schema and the
                -- migration actor to be able to SET ROLE to the new owner.
                EXECUTE 'GRANT CREATE ON SCHEMA public TO atlas_ledger_trigger_owner';
                EXECUTE format(
                    'GRANT %I TO %I WITH ADMIN OPTION',
                    role_name,
                    migration_role
                );
                EXECUTE
                    'ALTER FUNCTION public.atlas_ledger_journal_deferred_check() '
                    'OWNER TO atlas_ledger_trigger_owner';
                EXECUTE format('REVOKE %I FROM %I', role_name, migration_role);
                EXECUTE 'REVOKE CREATE ON SCHEMA public FROM atlas_ledger_trigger_owner';
            END IF;

            -- The trigger wrapper runs with the narrowly scoped owner above.
            -- Its fixed search_path prevents caller-controlled object shadowing.
            EXECUTE
                'ALTER FUNCTION public.atlas_ledger_journal_deferred_check() SECURITY DEFINER';
            EXECUTE
                'ALTER FUNCTION public.atlas_ledger_journal_deferred_check() '
                'SET search_path = pg_catalog, public';
            EXECUTE
                'ALTER FUNCTION public.atlas_validate_ledger_journal(integer) '
                'SET search_path = pg_catalog, public';

            -- Explicitly revoke the built-in PUBLIC EXECUTE grant and any
            -- direct grants inherited from managed/default object creators.
            FOREACH function_signature IN ARRAY ARRAY[
                'public.atlas_block_ledger_mutation()',
                'public.atlas_ledger_journal_deferred_check()',
                'public.atlas_validate_ledger_journal(integer)',
                'public.atlas_research_trial_archive_immutable()',
                'public.atlas_research_holdout_registry_immutable()'
            ] LOOP
                IF to_regprocedure(function_signature) IS NULL THEN
                    RAISE EXCEPTION 'required internal trigger function is missing: %', function_signature;
                END IF;

                EXECUTE format(
                    'REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC',
                    function_signature
                );

                FOREACH grantee_name IN ARRAY ARRAY[
                    'anon', 'authenticated', 'service_role',
                    'atlas_paper_reader', 'atlas_customer_paper',
                    'atlas_api_runtime', 'atlas_audit_reader'
                ] LOOP
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = grantee_name) THEN
                        EXECUTE format(
                            'REVOKE EXECUTE ON FUNCTION %s FROM %I',
                            function_signature,
                            grantee_name
                        );
                    END IF;
                END LOOP;
            END LOOP;

            -- Only the internal trigger owner may call the validator helper.
            EXECUTE
                'GRANT EXECUTE ON FUNCTION public.atlas_validate_ledger_journal(integer) '
                'TO atlas_ledger_trigger_owner';

            IF has_schema_privilege(role_name, 'public', 'CREATE')
               OR NOT has_schema_privilege(role_name, 'public', 'USAGE')
               OR NOT has_table_privilege(role_name, 'public.ledger_journals', 'SELECT')
               OR NOT has_table_privilege(role_name, 'public.ledger_journal_lines', 'SELECT')
               OR has_table_privilege(role_name, 'public.ledger_journals', 'INSERT')
               OR has_table_privilege(role_name, 'public.ledger_journals', 'UPDATE')
               OR has_table_privilege(role_name, 'public.ledger_journals', 'DELETE')
               OR has_table_privilege(role_name, 'public.ledger_journal_lines', 'INSERT')
               OR has_table_privilege(role_name, 'public.ledger_journal_lines', 'UPDATE')
               OR has_table_privilege(role_name, 'public.ledger_journal_lines', 'DELETE')
               OR NOT has_function_privilege(
                    role_name,
                    'public.atlas_validate_ledger_journal(integer)',
                    'EXECUTE'
               ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner does not match its required schema/table/function allowlist';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM pg_auth_members
                WHERE roleid = role_oid OR member = role_oid
            ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner unexpectedly has a role membership after ownership transfer';
            END IF;

            IF (
                SELECT count(*)
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public'
                  AND p.proowner = role_oid
            ) <> 1
               OR NOT EXISTS (
                    SELECT 1
                    FROM pg_proc p
                    WHERE p.oid = to_regprocedure('public.atlas_ledger_journal_deferred_check()')
                      AND p.proowner = role_oid
                      AND p.prosecdef
                      AND p.proconfig @> ARRAY['search_path=pg_catalog, public']
               ) THEN
                RAISE EXCEPTION
                    'atlas_ledger_trigger_owner must own only the fixed-search-path deferred trigger function';
            END IF;
        END
        $atlas_ledger_trigger_owner$;
    """))


def upgrade() -> None:
    # Existing rows predate sequence/hash capture. The default explicitly labels
    # them legacy and unchained; no attempt is made to reconstruct missing history.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names(schema="public" if bind.dialect.name == "postgresql" else None))
    archive_columns = {
        column["name"] for column in inspector.get_columns(
            "research_trial_archive",
            schema="public" if bind.dialect.name == "postgresql" else None,
        )
    }
    new_columns = (
        sa.Column("ledger_sequence", sa.BigInteger(), nullable=True),
        sa.Column("ledger_previous_sha256", sa.String(64), nullable=True),
        sa.Column("ledger_entry_sha256", sa.String(64), nullable=True),
        sa.Column("ledger_hmac_sha256", sa.String(64), nullable=True),
        sa.Column("ledger_hmac_key_id", sa.String(100), nullable=True),
        sa.Column(
            "ledger_status",
            sa.String(40),
            nullable=False,
            server_default="LEGACY_UNCHAINED",
        ),
    )
    for column in new_columns:
        if column.name not in archive_columns:
            op.add_column(
                "research_trial_archive",
                column,
                schema="public" if bind.dialect.name == "postgresql" else None,
            )
    index_names = {
        item["name"] for item in inspector.get_indexes(
            "research_trial_archive",
            schema="public" if bind.dialect.name == "postgresql" else None,
        )
    }
    if "uq_research_trial_ledger_sequence" not in index_names:
        op.create_index(
            "uq_research_trial_ledger_sequence",
            "research_trial_archive",
            ["ledger_sequence"],
            unique=True,
            schema="public" if bind.dialect.name == "postgresql" else None,
        )

    if "research_trial_ledger_head" not in existing_tables:
        op.create_table(
            "research_trial_ledger_head",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("last_sequence", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("last_entry_sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("id = 1", name="ck_research_trial_ledger_head_singleton"),
            schema="public" if bind.dialect.name == "postgresql" else None,
        )
    if "research_holdout_registry" not in existing_tables:
        op.create_table(
            "research_holdout_registry",
            sa.Column("scope_sha256", sa.String(64), primary_key=True),
            sa.Column("holdout_version", sa.String(100), nullable=False),
            sa.Column("source_sample_sha256", sa.String(64), nullable=False),
            sa.Column("report_sha256", sa.String(64), nullable=False),
            sa.Column("holdout_plan_json", sa.Text(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            schema="public" if bind.dialect.name == "postgresql" else None,
        )
    head = bind.execute(sa.text(
        "SELECT count(*) FROM research_trial_ledger_head WHERE id = 1"
    )).scalar_one()
    if not head:
        bind.execute(sa.text(
            "INSERT INTO research_trial_ledger_head "
            "(id, last_sequence, last_entry_sha256) VALUES (1, 0, '')"
        ))
    _secure_service_tables(bind)
    _secure_ledger_journal_trigger_owner(bind)


def downgrade() -> None:
    # This revision is forward-only because downgrading the schema would either
    # destroy one-time holdout consumption or make the trial chain unverifiable.
    # upgrade() is intentionally idempotent so CI downgrade/upgrade round-trips
    # can safely revisit this retained schema without recreating existing objects.
    pass

