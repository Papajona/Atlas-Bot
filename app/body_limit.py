"""Pure-ASGI request body size limit with slow-client protection.

Reject oversized payloads before framework parsers can start a response. Bodies
are buffered only for methods that may legitimately be parsed, and idle/total
deadlines prevent clients from holding routes open by dripping small chunks.
"""
from __future__ import annotations

import asyncio


class BodySizeLimitMiddleware:
    def __init__(
        self,
        app,
        max_bytes: int = 2_097_152,
        *,
        idle_timeout_seconds: float = 5.0,
        total_timeout_seconds: float = 30.0,
    ) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)
        self.idle_timeout_seconds = max(0.001, float(idle_timeout_seconds))
        self.total_timeout_seconds = max(
            self.idle_timeout_seconds, float(total_timeout_seconds)
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        http_version = str(scope.get("http_version") or "1.1")
        close_connection = http_version not in {"2", "3"}

        # Reject declared oversize before touching the streamed body.
        for name, value in scope.get("headers") or []:
            if name.lower() != b"content-length":
                continue
            try:
                declared = int(value)
            except (TypeError, ValueError):
                continue
            if declared > self.max_bytes:
                await self._reject(send, close_connection=close_connection, status=413)
                return

        # These methods have no Atlas request-body consumers. Do not let unused
        # streamed payloads hold public or authenticated handlers before routing.
        if str(scope.get("method") or "GET").upper() in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            await self.app(scope, receive, send)
            return

        buffered = bytearray()
        saw_request = False
        disconnected = False
        extension_message = None
        request_complete = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.total_timeout_seconds

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._reject(send, close_connection=close_connection, status=408)
                return
            try:
                message = await asyncio.wait_for(
                    receive(),
                    timeout=min(self.idle_timeout_seconds, remaining),
                )
            except asyncio.TimeoutError:
                await self._reject(send, close_connection=close_connection, status=408)
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
                await self._reject(send, close_connection=close_connection, status=413)
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
    async def _reject(send, *, close_connection: bool, status: int) -> None:
        body = (
            b'{"detail":"Request body too large"}'
            if status == 413
            else b'{"detail":"Request body timed out"}'
        )
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
