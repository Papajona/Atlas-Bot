#!/usr/bin/env python3
"""Legacy entry point for the offline Atlas Native AI check; no provider calls."""
from scripts.check_native_ai import main

if __name__ == "__main__":
    raise SystemExit(main())
