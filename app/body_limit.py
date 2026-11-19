"""Pure-ASGI request body size limit.

Reject oversized request bodies before application middleware, route handlers, or
framework parsers can start a response. Both Content-Length and streamed (chunked)
request bodies are bounded. At most max_bytes of body data is retained.

This middleware is dependency-free and can be tested with a stub ASGI app.
"""
from __future__ import annotations


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = 2_097_152) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        http_version = str(scope.get("http_version") or "1.1")
        close_connection = http_version not in {"2", "3"}

        # Check every Content-Length instance. Streaming byte accounting below
        # remains authoritative for absent, invalid, or understated lengths.
        for name, value in scope.get("headers") or []:
            if name.lower() != b"content-length":
                continue
            try:
                declared = int(value)
            except (TypeError, ValueError):
                continue
            if declared > self.max_bytes:
                await self._reject(send, close_connection=close_connection)
                return

        # Fully check the body before invoking the app. Raising from receive()
        # after framework parsing begins can leak a 400/500 instead of HTTP 413.
        # A single concatenated ASGI request message also bounds memory when an
        # attacker sends an unbounded number of empty chunks.
        buffered = bytearray()
        disconnected = False
        other_message = None
        request_complete = False
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                disconnected = True
                break
            if message_type != "http.request":
                other_message = message
                break

            buffered.extend(message.get("body") or b"")
            if len(buffered) > self.max_bytes:
                await self._reject(send, close_connection=close_connection)
                return
            if not message.get("more_body", False):
                request_complete = True
                break

        first_message_sent = False
        other_message_sent = False

        async def replay_receive():
            nonlocal first_message_sent, other_message_sent
            if not first_message_sent:
                first_message_sent = True
                return {
                    "type": "http.request",
                    "body": bytes(buffered),
                    "more_body": not request_complete,
                }
            if disconnected:
                return {"type": "http.disconnect"}
            if other_message is not None and not other_message_sent:
                other_message_sent = True
                return other_message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send, *, close_connection: bool) -> None:
        body = b'{"detail":"Request body too large"}'
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        # Connection-specific headers are forbidden on HTTP/2 and HTTP/3.
        if close_connection:
            headers.append((b"connection", b"close"))
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": headers,
        })
        await send({
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        })
