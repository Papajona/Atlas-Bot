"""Pure role-policy checks for the public-schema RLS lockdown.

Keep this module free of database and application imports so its policy can be
unit-tested by lightweight CI jobs that do not install SQLAlchemy.
"""


def require_rls_bypass_role(
    role_name: str,
    is_superuser: bool,
    bypass_rls: bool,
    owns_all_rls_tables: bool = False,
) -> None:
    """Reject roles that would be blocked by RLS on public tables."""
    if not (is_superuser or bypass_rls or owns_all_rls_tables):
        raise RuntimeError(
            "Database role is not authorized to bypass row-level security: "
            f"role={role_name!r}. Migration 0049 enables RLS on public tables; "
            "configure DATABASE_URL with the approved trusted server-side PostgreSQL role."
        )
