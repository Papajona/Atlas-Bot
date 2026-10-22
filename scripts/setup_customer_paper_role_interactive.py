#!/usr/bin/env python3
"""Create the restricted customer-paper role using psql's native password prompt.

This avoids persisting the administrative password in a URL, environment variable,
or .pgpass file. The generated runtime URL is kept in a mode-0600 file so the
subsequent staging bootstrap can upload it directly to Secret Manager.
"""
from __future__ import annotations

import argparse
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

POOLER_HOST = "aws-0-eu-west-1.pooler.supabase.com"
RUNTIME_ROLE = "atlas_customer_paper"
STAGING_PROJECT_REF = "wauaelzyugjuepmrewaw"



def validate_project_ref(project_ref: str) -> str:
    """Fail closed unless setup targets the one confirmed existing staging project."""
    normalized = str(project_ref or "").strip()
    if (
        len(normalized) != 20
        or not normalized.isascii()
        or not normalized.isalnum()
        or normalized.lower() != normalized
    ):
        raise ValueError("Project ref must be the exact lowercase 20-character Supabase ref")
    if normalized != STAGING_PROJECT_REF:
        raise ValueError("Project ref does not match the confirmed existing staging project")
    return normalized


def _create_url_file_atomically(url_file: Path, uri: str) -> None:
    """Publish a complete mode-0600 URL without exposing partial final-path writes."""
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{url_file.name}.", suffix=".tmp", dir=str(url_file.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(uri + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory hard-link publication is atomic and fails if final already exists.
        # Unlike os.replace(), os.link() never overwrites a competing final file.
        os.link(temporary, url_file)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    else:
        # On failure the final path, if published, still contains the complete fsynced URL.
        temporary.unlink()

def validate_runtime_url(uri: str, project_ref: str, expected_host: str = POOLER_HOST) -> str:
    """Return the decoded role password after validating the full expected endpoint."""
    parsed = urlsplit(uri)
    if parsed.scheme != "postgresql+asyncpg":
        raise ValueError("Runtime URL must use postgresql+asyncpg")
    if parsed.hostname != expected_host or parsed.port != 5432 or parsed.path != "/postgres":
        raise ValueError("Runtime URL must target the approved session pooler on port 5432 and database postgres")
    if unquote(parsed.username or "") != f"{RUNTIME_ROLE}.{project_ref}":
        raise ValueError("Runtime URL username does not match the configured project-bound paper role")
    if not parsed.password:
        raise ValueError("Runtime URL password is missing")
    if parse_qs(parsed.query).get("ssl") != ["require"]:
        raise ValueError("Runtime URL must require TLS")
    return unquote(parsed.password)


def role_setup_sql(runtime_credential: str) -> str:
    """Build one transactional, guarded role setup; never print this SQL."""
    if not runtime_credential:
        raise ValueError("Role credential cannot be empty")
    escaped = runtime_credential.replace("'", "''")
    return f"""
\\set ON_ERROR_STOP on
BEGIN;

DO $guard$
DECLARE can_manage_roles boolean;
BEGIN
  IF current_database() <> 'postgres'
     OR current_setting('transaction_read_only') <> 'off' THEN
    RAISE EXCEPTION 'Wrong database or read-only connection';
  END IF;

  SELECT (rolsuper OR rolcreaterole)
    INTO can_manage_roles
    FROM pg_roles
    WHERE rolname = current_user;
  IF NOT COALESCE(can_manage_roles, false) THEN
    RAISE EXCEPTION 'Connected role cannot create restricted roles';
  END IF;

  IF NOT has_database_privilege(current_user, current_database(), 'CREATE') THEN
    RAISE EXCEPTION 'Connected role lacks database CREATE privilege';
  END IF;

  IF current_user IN (
    'atlas_customer_paper', 'anon', 'authenticated', 'service_role',
    'supabase_admin', 'supabase_auth_admin', 'authenticator'
  ) THEN
    RAISE EXCEPTION 'Connected role is not an approved operator';
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'atlas_customer_paper') THEN
    RAISE EXCEPTION 'atlas_customer_paper already exists; inspect it instead of replacing its password';
  END IF;
END
$guard$;

CREATE ROLE atlas_customer_paper
  LOGIN NOINHERIT NOBYPASSRLS
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

-- PostgreSQL 16+ may give the CREATEROLE role membership in a role it creates.
-- Supabase owns the control-plane membership grant; do not try to revoke it here.
ALTER ROLE atlas_customer_paper PASSWORD '{escaped}';
GRANT CONNECT ON DATABASE postgres TO atlas_customer_paper;

DO $verify$
DECLARE role_id oid;
BEGIN
  SELECT oid INTO role_id
    FROM pg_roles
    WHERE rolname = 'atlas_customer_paper'
      AND rolcanlogin
      AND NOT rolsuper
      AND NOT rolinherit
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolreplication
      AND NOT rolbypassrls;

  IF role_id IS NULL THEN
    RAISE EXCEPTION 'Restricted role attribute verification failed';
  END IF;

  IF EXISTS (
    SELECT 1 FROM pg_auth_members
    WHERE member = role_id
  ) THEN
    RAISE EXCEPTION 'Paper role must not inherit privileges from another role';
  END IF;

  IF (SELECT count(*) FROM pg_auth_members WHERE roleid = role_id) > 1
     OR EXISTS (
       SELECT 1
       FROM pg_auth_members m
       JOIN pg_roles member_role ON member_role.oid = m.member
       JOIN pg_roles grantor_role ON grantor_role.oid = m.grantor
       WHERE m.roleid = role_id
         AND NOT (
           member_role.rolname = 'postgres'
           AND grantor_role.rolname = 'supabase_admin'
           AND m.admin_option
           AND (to_jsonb(m) ? 'inherit_option')
           AND COALESCE((to_jsonb(m)->>'inherit_option')::boolean, false) = false
           AND (to_jsonb(m) ? 'set_option')
           AND COALESCE((to_jsonb(m)->>'set_option')::boolean, false) = false
         )
     ) THEN
    RAISE EXCEPTION 'Paper role has an unsafe incoming membership';
  END IF;
END
$verify$;

COMMIT;
SELECT 'PASS: restricted atlas_customer_paper role created and verified';
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-ref", required=True, help="Exact existing 20-character Supabase project ref")
    parser.add_argument(
        "--url-file",
        default=str(Path.home() / ".atlas-customer-paper" / "database-url"),
        help="Private runtime URL file (default: ~/.atlas-customer-paper/database-url)",
    )
    args = parser.parse_args()

    project_ref = validate_project_ref(args.project_ref)

    url_file = Path(args.url_file).expanduser()
    parent = url_file.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or parent.stat().st_mode & 0o077:
        raise RuntimeError("Credential directory must be a real private directory with mode 0700 or stricter")
    if url_file.is_symlink():
        raise RuntimeError("Refusing to follow a symlink for the runtime URL file")

    if url_file.exists():
        if not url_file.is_file() or stat.S_IMODE(url_file.stat().st_mode) & 0o077:
            raise RuntimeError("Existing runtime URL file must be a regular file with mode 0600 or stricter")
        uri = url_file.read_text(encoding="utf-8").strip()
        runtime_credential = validate_runtime_url(uri, project_ref)
        print("Reusing the existing protected runtime URL file; its contents are withheld.")
    else:
        runtime_credential = secrets.token_urlsafe(48)
        uri = urlunsplit((
            "postgresql+asyncpg",
            f"{RUNTIME_ROLE}.{project_ref}:{quote(runtime_credential, safe='')}@{POOLER_HOST}:5432",
            "/postgres",
            "ssl=require",
            "",
        ))
        _create_url_file_atomically(url_file, uri)
        print("Created a protected runtime URL file; its contents are withheld.")

    with tempfile.TemporaryDirectory(prefix="atlas-paper-role-") as temp_dir:
        sql_path = Path(temp_dir) / "role-setup.sql"
        sql_fd = os.open(sql_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(sql_fd, "w", encoding="utf-8") as stream:
            stream.write(role_setup_sql(runtime_credential))

        # Force native interactive password prompting. Do not inherit saved password
        # env vars, .pgpass overrides, or service definitions from earlier attempts.
        env = os.environ.copy()
        for key in (
            "PGPASSWORD", "PGPASSFILE", "PGHOSTADDR", "PGSERVICE",
            "PGSERVICEFILE", "PGOPTIONS", "DATABASE_URL", "ATLAS_ADMIN_DATABASE_URL",
        ):
            env.pop(key, None)

        conninfo = (
            f"host={POOLER_HOST} port=5432 dbname=postgres "
            f"user=postgres.{project_ref} sslmode=require"
        )
        print("Connect with the existing database password at the native psql prompt.")
        print("The administrative password is not stored in this script or a credential file.")
        result = subprocess.run(
            [
                "psql", conninfo, "-W", "-X", "-v", "ON_ERROR_STOP=1",
                "-f", str(sql_path),
            ],
            env=env,
            check=False,
        )

    if result.returncode != 0:
        print(
            "BLOCKED: role setup did not return success. The URL file is preserved for safe retry; "
            "check the database role state before retrying.",
            file=sys.stderr,
        )
        return result.returncode or 2

    print("PASS: restricted paper role is created and verified.")
    print(f"Protected runtime URL file: {url_file}")
    print("Next step: create atlas_paper_reader, then apply the reviewed paper schema. Trading remains disabled.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2)
