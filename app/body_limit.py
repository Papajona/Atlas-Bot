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

        # Buffer before invoking the app. Raising from receive() after the app
        # starts can be translated by framework/body-parsing middleware into
        # 400/500; pre-buffering guarantees an oversized chunked request gets 413.
        messages = []
        received = 0
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                messages.append(message)
                break
            if message_type != "http.request":
                messages.append(message)
                continue

            received += len(message.get("body") or b"")
            if received > self.max_bytes:
                await self._reject(send, close_connection=close_connection)
                return

            messages.append(message)
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
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
