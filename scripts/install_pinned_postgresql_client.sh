#!/usr/bin/env bash
set -euo pipefail

# Restore and audit must use the same PostgreSQL 17 archive tooling.
# Keep this major pinned in one script rather than using Ubuntu's unversioned
# postgresql-client metapackage (which can select a different major).
readonly POSTGRES_CLIENT_MAJOR=17
readonly POSTGRES_BIN_DIR="/usr/lib/postgresql/$POSTGRES_CLIENT_MAJOR/bin"
readonly KEYRING="/usr/share/keyrings/postgresql-pgdg.gpg"
readonly SOURCE_LIST="/etc/apt/sources.list.d/postgresql-pgdg.list"

sudo apt-get update
sudo apt-get install --yes --no-install-recommends ca-certificates curl gnupg

curl --fail --silent --show-error --location \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  | sudo gpg --dearmor --yes --output "$KEYRING"

source /etc/os-release
: "$VERSION_CODENAME"
printf 'deb [signed-by=%s] https://apt.postgresql.org/pub/repos/apt %s-pgdg main\n' \
  "$KEYRING" "$VERSION_CODENAME" \
  | sudo tee "$SOURCE_LIST" >/dev/null

sudo apt-get update
sudo apt-get install --yes --no-install-recommends "postgresql-client-$POSTGRES_CLIENT_MAJOR"

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

echo "PASS: pinned PostgreSQL 17 archive tools installed."
echo "$dump_version"
echo "$restore_version"
