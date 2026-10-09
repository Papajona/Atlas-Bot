#!/usr/bin/env python3
"""Safely verify Deriv API-token authentication without placing or changing trades.

Exit codes: 0 authenticated, 1 authentication/network failure, 2 required configuration missing.
The token and full authorization payload are never printed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ENDPOINT = "wss://ws.derivws.com/websockets/v3"


async def verify(app_id: int, token: str) -> bool:
    import websockets

    url = f"{ENDPOINT}?app_id={app_id}"
    async with websockets.connect(
        url, open_timeout=10, close_timeout=5, max_size=1_000_000
    ) as ws:
        await asyncio.wait_for(
            ws.send(json.dumps({"authorize": token, "req_id": 1})), timeout=10
        )
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            response = json.loads(raw)
            if response.get("req_id") != 1 and response.get("echo_req", {}).get("req_id") != 1:
                continue
            if response.get("error"):
                error = response["error"]
                # Provider error code/message are useful; never print request payloads.
                print(f"Deriv authentication: FAIL ({error.get('code', 'provider_error')})")
                return False
            auth = response.get("authorize")
            if isinstance(auth, dict) and auth.get("loginid"):
                print("Deriv authentication: PASS (token accepted; no trade submitted).")
                return True
            print("Deriv authentication: FAIL (response lacked authorization identity).")
            return False


def main() -> int:
    raw_app_id = os.environ.get("DERIV_APP_ID", "").strip()
    token = os.environ.get("DERIV_API_TOKEN", "").strip()
    if not raw_app_id or not token:
        missing = []
        if not raw_app_id:
            missing.append("DERIV_APP_ID")
        if not token:
            missing.append("DERIV_API_TOKEN")
        print("Deriv authentication: NOT VERIFIED (missing " + ", ".join(missing) + ").")
        return 2
    try:
        app_id = int(raw_app_id)
        if app_id <= 0:
            raise ValueError
    except ValueError:
        print("Deriv authentication: FAIL (DERIV_APP_ID must be a positive integer).")
        return 1
    try:
        return 0 if asyncio.run(verify(app_id, token)) else 1
    except Exception as exc:
        # Avoid exception details that could contain request/session data.
        print(f"Deriv authentication: FAIL ({type(exc).__name__}; details suppressed).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
