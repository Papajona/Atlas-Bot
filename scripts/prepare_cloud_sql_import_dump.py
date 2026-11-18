#!/usr/bin/env python3
"""Prepare supported SQL from plain SQL, gzip SQL, or a PostgreSQL custom archive."""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

_CUSTOM_MAGIC = b"PGDMP"
_GZIP_MAGIC = b"\x1f\x8b"


def _prefix(path: Path, size: int = 5) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def _gzip_payload_magic(path: Path) -> bytes:
    with gzip.open(path, "rb") as handle:
        return handle.read(5)


def _validate_gzip_stream(path: Path) -> None:
    total = 0
    with gzip.open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
    if total == 0:
        raise ValueError("gzip SQL backup decompresses to an empty file")


def _run_pg_restore(
    archive_path: Path,
    output_path: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Validate and convert a custom archive into plain SQL without connecting."""
    listing = runner(
        ["pg_restore", "--list", str(archive_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(listing, "returncode", 1) != 0:
        raise RuntimeError("pg_restore could not read the PostgreSQL custom archive")

    command = [
        "pg_restore",
        "--no-owner",
        "--no-privileges",
        "--no-comments",
        "--exit-on-error",
        f"--file={output_path}",
        str(archive_path),
    ]
    result = runner(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(result, "returncode", 1) != 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("pg_restore could not convert the PostgreSQL custom archive to SQL")
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("pg_restore produced an empty SQL import")


def prepare_import_dump(
    input_path: str | Path,
    output_path: str | Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Convert custom PostgreSQL archives; preserve supported plain/gzip SQL bytes."""
    source = Path(input_path)
    destination = Path(output_path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("source backup is missing or empty")
    if source.resolve() == destination.resolve():
        raise ValueError("prepared SQL output must not overwrite the source backup")
    destination.parent.mkdir(parents=True, exist_ok=True)
    magic = _prefix(source)

    if magic == _CUSTOM_MAGIC:
        input_format = "postgresql_custom_archive"
        _run_pg_restore(source, destination, runner=runner)
        output_format = "plain_sql"
        converted = True
    elif magic.startswith(_GZIP_MAGIC):
        payload_magic = _gzip_payload_magic(source)
        if payload_magic == _CUSTOM_MAGIC:
            input_format = "gzip_postgresql_custom_archive"
            with tempfile.TemporaryDirectory(prefix="atlas-pg-restore-") as temp_dir:
                archive_path = Path(temp_dir) / "source.dump"
                with gzip.open(source, "rb") as compressed, archive_path.open("wb") as expanded:
                    shutil.copyfileobj(compressed, expanded, length=1024 * 1024)
                _run_pg_restore(archive_path, destination, runner=runner)
            output_format = "plain_sql"
            converted = True
        else:
            _validate_gzip_stream(source)
            input_format = "gzip_sql"
            shutil.copyfile(source, destination)
            output_format = "gzip_sql"
            converted = False
    else:
        input_format = "plain_sql_candidate"
        shutil.copyfile(source, destination)
        output_format = "plain_sql_candidate"
        converted = False

    if not destination.is_file() or destination.stat().st_size <= 0:
        destination.unlink(missing_ok=True)
        raise ValueError("prepared SQL output is missing or empty")
    return {
        "status": "PASS",
        "input_format": input_format,
        "output_format": output_format,
        "converted": converted,
        "output_bytes": destination.stat().st_size,
        "database_connection_attempted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = prepare_import_dump(args.input, args.output)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        Path(args.output).unlink(missing_ok=True)
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "POSTGRESQL_IMPORT_PREPARATION_FAILED",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
