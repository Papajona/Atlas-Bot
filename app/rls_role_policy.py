"""Pure role-policy checks for the public-schema RLS lockdown.

Keep this module free of database and application imports so its policy can be
unit-tested by lightweight CI jobs that do not install SQLAlchemy.
"""

_RESERVED_RUNTIME_ROLES = {
    "postgres",
    "service_role",
    "supabase_admin",
    "supabase_auth_admin",
    "authenticator",
    "anon",
    "authenticated",
}


def require_rls_bypass_role(
    role_name: str,
    is_superuser: bool,
    bypass_rls: bool,
    owned_public_table_count: int = 0,
    can_create_roles: bool = False,
    can_create_databases: bool = False,
    can_replicate: bool = False,
    role_membership_count: int = 0,
) -> None:
    """Require a dedicated non-owner/non-admin server role for the backend-only DB path.

    Migration 0049 enables RLS without generic policies and revokes public API schema
    access. Therefore the trusted application connection needs BYPASSRLS, but it must
    not be a superuser, owner of application tables, role/database administrator, or
    a built-in Supabase gateway/service role. Those powers belong to operators, not
    the deployed API/worker.
    """
    normalized_role = str(role_name or "").strip().lower()
    invalid = (
        normalized_role in _RESERVED_RUNTIME_ROLES
        or is_superuser
        or not bypass_rls
        or owned_public_table_count > 0
        or can_create_roles
        or can_create_databases
        or can_replicate
        or role_membership_count > 0
    )
    if invalid:
        raise RuntimeError(
            "Database role is not authorized to bypass row-level security with least privilege: "
            f"role={role_name!r}. Use a dedicated non-owner, non-admin runtime role with "
            "BYPASSRLS and explicit application-table grants; do not use postgres, a "
            "Supabase gateway/service role, a table-owner role, or an administrative role or a role with inherited memberships."
        )


def require_public_schema_rls_complete(public_table_count: int, tables_without_rls: int) -> None:
    """Fail closed if public tables are missing RLS after the lockdown migration."""
    if public_table_count < 1 or tables_without_rls != 0:
        raise RuntimeError(
            "Public-schema RLS lockdown is incomplete: "
            f"public_tables={public_table_count}, tables_without_rls={tables_without_rls}. "
            "Enable and verify RLS on every public table before starting Atlas."
        )
