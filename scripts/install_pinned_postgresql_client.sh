#!/usr/bin/env bash
set -euo pipefail

# Restore and audit must use the same PostgreSQL 17 archive tooling.
# Pin the same PostgreSQL minor release used by the verified recovery CI run.
# Fail closed when PGDG no longer offers that release; never silently drift.
readonly POSTGRES_CLIENT_MAJOR=17
readonly POSTGRES_CLIENT_RELEASE=17.12
readonly POSTGRES_BIN_DIR="/usr/lib/postgresql/$POSTGRES_CLIENT_MAJOR/bin"
readonly KEYRING="/usr/share/keyrings/postgresql-pgdg.gpg"
readonly SOURCE_LIST="/etc/apt/sources.list.d/postgresql-pgdg.list"
readonly ARCHIVE_SOURCE_LIST="/etc/apt/sources.list.d/postgresql-pgdg-archive.list"
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

find_pinned_package_version() {
  apt-cache madison "postgresql-client-$POSTGRES_CLIENT_MAJOR" \
    | awk -v prefix="${POSTGRES_CLIENT_RELEASE}-" 'index($3, prefix) == 1 { print $3; exit }'
}

apt_with_retries update
package_version="$(find_pinned_package_version)"

if [[ -z "$package_version" ]]; then
  # PGDG's main suite rotates older package builds out. The signed archive
  # retains historical releases; use the same PGDG signing key and never
  # relax signature verification or silently advance to a newer minor.
  echo "PostgreSQL $POSTGRES_CLIENT_RELEASE is absent from PGDG main; checking the signed PGDG archive." >&2
  printf 'deb [signed-by=%s] https://apt-archive.postgresql.org/pub/repos/apt %s-pgdg-archive main\n' \
    "$KEYRING" "$VERSION_CODENAME" \
    | sudo tee "$ARCHIVE_SOURCE_LIST" >/dev/null
  apt_with_retries update
  package_version="$(find_pinned_package_version)"
fi

if [[ -z "$package_version" ]]; then
  echo "BLOCKED: PostgreSQL $POSTGRES_CLIENT_RELEASE client package is unavailable from both signed PGDG main and archive suites." >&2
  exit 1
fi

echo "Installing PostgreSQL $POSTGRES_CLIENT_RELEASE client package version $package_version."
# Keep apt's default no-downgrade policy. If the pinned major-17 client is
# older than the installed client package, fail closed rather than downgrading it.
client_package="postgresql-client-${POSTGRES_CLIENT_MAJOR}"
installed_package_status="$(dpkg-query -W -f='${db:Status-Status}' "$client_package" 2>/dev/null || true)"
if [[ "$installed_package_status" == "installed" ]]; then
  installed_package_version="$(dpkg-query -W -f='${Version}' "$client_package")"
  if dpkg --compare-versions "$installed_package_version" gt "$package_version"; then
    echo "BLOCKED: installed $client_package version $installed_package_version is newer than pinned $package_version; refusing to downgrade this client package." >&2
    exit 1
  fi
fi
# --no-remove prevents apt from solving conflicts by removing unrelated packages.
# Exact-version installation and the pg_dump/pg_restore checks below still
# enforce the PostgreSQL 17.12 tooling contract.
apt_with_retries install --yes --no-install-recommends --no-remove \
  "$client_package=${package_version}"

test -x "$POSTGRES_BIN_DIR/pg_restore"
test -x "$POSTGRES_BIN_DIR/pg_dump"
restore_version="$("$POSTGRES_BIN_DIR/pg_restore" --version)"
dump_version="$("$POSTGRES_BIN_DIR/pg_dump" --version)"
if ! grep -Eq '\(PostgreSQL\) 17\.12([[:space:]]|$)' <<<"$restore_version"; then
  echo "BLOCKED: expected PostgreSQL 17.12 pg_restore, got: $restore_version" >&2
  exit 1
fi
if ! grep -Eq '\(PostgreSQL\) 17\.12([[:space:]]|$)' <<<"$dump_version"; then
  echo "BLOCKED: expected PostgreSQL 17.12 pg_dump, got: $dump_version" >&2
  exit 1
fi

if [[ -v GITHUB_PATH ]] && [[ -n "$GITHUB_PATH" ]]; then
  printf '%s\n' "$POSTGRES_BIN_DIR" >> "$GITHUB_PATH"
fi
export PATH="$POSTGRES_BIN_DIR:$PATH"

echo "PASS: PostgreSQL 17.12 archive tools installed and release-pinned."
echo "$dump_version"
echo "$restore_version"
