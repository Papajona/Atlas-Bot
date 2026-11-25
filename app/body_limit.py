"""Pure-ASGI request body size limit with bounded pre-routing reads."""
from __future__ import annotations

import asyncio
import math
import time


class BodySizeLimitMiddleware:
    """Reject oversize bodies before parsing without allowing slow clients to pin workers.

    Body methods are buffered so a streamed oversize request still receives a
    deterministic 413 before a framework parser can start a response. Reads have
    both idle and total deadlines. Methods that must not carry request bodies are
    rejected immediately when they declare one and are never pre-buffered.
    """

    _BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(
        self,
        app,
        max_bytes: int = 2_097_152,
        *,
        body_idle_timeout_seconds: float = 2.0,
        body_total_timeout_seconds: float = 10.0,
    ) -> None:
        idle_timeout = float(body_idle_timeout_seconds)
        total_timeout = float(body_total_timeout_seconds)
        if not math.isfinite(idle_timeout) or idle_timeout <= 0.0:
            raise ValueError("body idle timeout must be finite and positive")
        if not math.isfinite(total_timeout) or total_timeout <= 0.0:
            raise ValueError("body total timeout must be finite and positive")
        if total_timeout < idle_timeout:
            raise ValueError("body total timeout must be greater than or equal to the idle timeout")

        self.app = app
        self.max_bytes = int(max_bytes)
        self.body_idle_timeout_seconds = max(0.01, idle_timeout)
        self.body_total_timeout_seconds = total_timeout

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        http_version = str(scope.get("http_version") or "1.1")
        close_connection = http_version not in {"2", "3"}
        headers = scope.get("headers") or []
        declared_lengths = []
        has_transfer_encoding = False

        for name, value in headers:
            lower_name = name.lower()
            if lower_name == b"transfer-encoding":
                has_transfer_encoding = True
            if lower_name != b"content-length":
                continue
            try:
                declared = int(value)
            except (TypeError, ValueError):
                continue
            if declared < 0:
                await self._reject(send, close_connection=close_connection, status=400, detail="Invalid Content-Length")
                return
            declared_lengths.append(declared)
            if declared > self.max_bytes:
                await self._reject(send, close_connection=close_connection)
                return

        # Content-Length ambiguity is rejected rather than interpreted differently
        # by this middleware and the downstream HTTP parser.
        if len(set(declared_lengths)) > 1:
            await self._reject(send, close_connection=close_connection, status=400, detail="Conflicting Content-Length headers")
            return
        if has_transfer_encoding and declared_lengths:
            await self._reject(
                send,
                close_connection=close_connection,
                status=400,
                detail="Conflicting Transfer-Encoding and Content-Length headers",
            )
            return

        method = str(scope.get("method") or "GET").upper()
        body_declared = any(length > 0 for length in declared_lengths) or has_transfer_encoding
        if method not in self._BODY_METHODS:
            if body_declared:
                await self._reject(
                    send,
                    close_connection=close_connection,
                    status=400,
                    detail="This request method does not accept a body",
                )
                return
            # Do not wait for a request body on bodyless methods or routes.
            await self.app(scope, receive, send)
            return

        # A request body can be slow even when every chunk stays below max_bytes.
        # Bound both the gap between chunks and the total time spent buffering.
        buffered = bytearray()
        saw_request = False
        disconnected = False
        extension_message = None
        request_complete = False
        deadline = time.monotonic() + self.body_total_timeout_seconds

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await self._reject(
                    send,
                    close_connection=close_connection,
                    status=408,
                    detail="Request body timed out",
                )
                return
            try:
                message = await asyncio.wait_for(
                    receive(),
                    timeout=min(self.body_idle_timeout_seconds, remaining),
                )
            except asyncio.TimeoutError:
                await self._reject(
                    send,
                    close_connection=close_connection,
                    status=408,
                    detail="Request body timed out",
                )
                return

            message_type = message.get("type")
            if message_type == "http.disconnect":
                disconnected = True
                break
            if message_type != "http.request":
                extension_message = message
                break

            saw_request = True
            chunk = message.get("body") or b""
            if len(buffered) + len(chunk) > self.max_bytes:
                await self._reject(send, close_connection=close_connection)
                return
            buffered.extend(chunk)

            if not message.get("more_body", False):
                request_complete = True
                break

        first_message_sent = False
        extension_message_sent = False

        async def replay_receive():
            nonlocal first_message_sent, extension_message_sent
            if not first_message_sent:
                first_message_sent = True
                if not saw_request and disconnected:
                    return {"type": "http.disconnect"}
                return {
                    "type": "http.request",
                    "body": bytes(buffered),
                    "more_body": not request_complete,
                }
            if disconnected:
                return {"type": "http.disconnect"}
            if extension_message is not None and not extension_message_sent:
                extension_message_sent = True
                return extension_message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(
        send,
        *,
        close_connection: bool,
        status: int = 413,
        detail: str = "Request body too large",
    ) -> None:
        import json

        body = json.dumps({"detail": detail}, separators=(",", ":")).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if close_connection:
            headers.append((b"connection", b"close"))
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        })
        await send({
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        })
