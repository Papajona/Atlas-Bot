"""Bound request-body reads for endpoints with stricter payload limits."""
from __future__ import annotations

from fastapi import HTTPException


async def read_limited_body(request, max_bytes: int) -> bytes:
    """Read a request incrementally and stop as soon as its byte limit is exceeded.

    Content-Length is an early-rejection hint only; the streamed byte count is
    authoritative for missing, understated, or chunked request bodies.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")

    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        if not declared_length.isascii() or not declared_length.isdecimal():
            raise HTTPException(400, "Invalid Content-Length")
        if int(declared_length) > max_bytes:
            raise HTTPException(413, "Webhook payload too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > max_bytes - len(body):
            raise HTTPException(413, "Webhook payload too large")
        body.extend(chunk)
    return bytes(body)
