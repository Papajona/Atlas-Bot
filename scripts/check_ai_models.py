#!/usr/bin/env python3
"""Legacy entry point for the offline Atlas Native AI check; no provider calls."""
from __future__ import annotations

import sys
from pathlib import Path

_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[1])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)

from scripts.check_native_ai import main

if __name__ == "__main__":
    raise SystemExit(main())
