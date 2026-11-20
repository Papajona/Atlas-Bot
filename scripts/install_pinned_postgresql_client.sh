#!/usr/bin/env bash
set -euo pipefail

# Restore and audit must use the same PostgreSQL 17 archive tooling.
# Keep this major pinned in one script rather than using Ubuntu's unversioned
# postgresql-client metapackage (which can select a different major).
readonly POSTGRES_CLIENT_MAJOR=17
readonly POSTGRES_CLIENT_RELEASE=17.12
readonly POSTGRES_BIN_DIR="/usr/lib/postgresql/$POSTGRES_CLIENT_MAJOR/bin"
readonly KEYRING="/usr/share/keyrings/postgresql-pgdg.gpg"
readonly SOURCE_LIST="/etc/apt/sources.list.d/postgresql-pgdg.list"
readonly APT_RETRIES=4

apt_with_retries() {
  local attempt=1 delay=3 output rc
  while (( attempt <= APT_RETRIES )); do
    if output="$(sudo apt-get \
      -o Acquire::Retries=3 \
      -o Acquire::http::Timeout=30 \
      -o Acquire::https::Timeout=30 \
      "$@" 2>&1)"; then
      rc=0
    else
      rc=$?
    fi

    printf '%s\n' "$output"
    # apt-get update can exit 0 while silently retaining stale indexes after
    # an unavailable mirror. Treat those warnings as a failed attempt so the
    # pinned client install never appears green on partial package metadata.
    if (( rc == 0 )) && ! grep -Eqi \
      'Failed to fetch|Some index files failed to download|Service Unavailable|HTTP (429|5[0-9][0-9])' \
      <<<"$output"; then
      return 0
    fi

    if (( attempt == APT_RETRIES )); then
      echo "BLOCKED: apt-get $* failed after $APT_RETRIES attempts." >&2
      return 1
    fi

    echo "Transient Ubuntu/PGDG package mirror failure (attempt $attempt/$APT_RETRIES); retrying in ${delay}s." >&2
    sleep "$delay"
    delay=$((delay * 2))
    attempt=$((attempt + 1))
  done
  return 1
}

apt_with_retries update
apt_with_retries install --yes --no-install-recommends ca-certificates curl gnupg

curl --fail --silent --show-error --location \
  --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 15 --max-time 120 \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  | sudo gpg --dearmor --yes --output "$KEYRING"

source /etc/os-release
: "$VERSION_CODENAME"
printf 'deb [signed-by=%s] https://apt.postgresql.org/pub/repos/apt %s-pgdg main\n' \
  "$KEYRING" "$VERSION_CODENAME" \
  | sudo tee "$SOURCE_LIST" >/dev/null

apt_with_retries update
package_version="$(
  apt-cache madison "postgresql-client-$POSTGRES_CLIENT_MAJOR" \
    | awk -v prefix="${POSTGRES_CLIENT_RELEASE}-" 'index($3, prefix) == 1 { print $3; exit }'
)"
if [[ -z "$package_version" ]]; then
  echo "BLOCKED: PostgreSQL $POSTGRES_CLIENT_RELEASE client package is not available from the configured PGDG repository." >&2
  exit 1
fi
apt_with_retries install --yes --no-install-recommends --allow-downgrades \
  "postgresql-client-${POSTGRES_CLIENT_MAJOR}=${package_version}"

test -x "$POSTGRES_BIN_DIR/pg_restore"
test -x "$POSTGRES_BIN_DIR/pg_dump"
restore_version="$("$POSTGRES_BIN_DIR/pg_restore" --version)"
dump_version="$("$POSTGRES_BIN_DIR/pg_dump" --version)"
if ! grep -Eq '\(PostgreSQL\) 17(\.|$)' <<<"$restore_version"; then
  echo "BLOCKED: expected PostgreSQL 17 pg_restore, got: $restore_version" >&2
  exit 1
fi
if ! grep -Eq '\(PostgreSQL\) 17(\.|$)' <<<"$dump_version"; then
  echo "BLOCKED: expected PostgreSQL 17 pg_dump, got: $dump_version" >&2
  exit 1
fi

if [[ -v GITHUB_PATH ]] && [[ -n "$GITHUB_PATH" ]]; then
  printf '%s\n' "$POSTGRES_BIN_DIR" >> "$GITHUB_PATH"
fi
export PATH="$POSTGRES_BIN_DIR:$PATH"

echo "PASS: PostgreSQL 17.12 archive tools installed and release-pinned."
echo "$dump_version"
echo "$restore_version"
