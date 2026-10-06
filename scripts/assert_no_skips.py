#!/usr/bin/env python3
"""Fail when mandatory pytest JUnit reports contain skipped tests."""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main(paths: list[str]) -> int:
    if not paths:
        print("ERROR: no JUnit XML reports supplied", file=sys.stderr)
        return 2

    total_skipped = 0
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            print(f"ERROR: missing JUnit report: {path}", file=sys.stderr)
            return 2
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            print(f"ERROR: invalid JUnit XML {path}: {exc}", file=sys.stderr)
            return 2
        skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in root.iter("testsuite"))
        total_skipped += skipped
        if skipped:
            print(f"ERROR: {path}: {skipped} skipped test(s)", file=sys.stderr)

    if total_skipped:
        return 1
    print("No skipped tests found in mandatory JUnit reports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
