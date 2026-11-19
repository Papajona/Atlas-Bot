"""Pure-ASGI request body size limit.

Reject oversized webhook, funding and API request bodies before handlers buffer them.
Both Content-Length and streamed (chunked) request bodies are bounded.

This middleware is dependency-free and can be tested with a stub ASGI app.
"""
from __future__ import annotations


class _BodyTooLarge(Exception):
    """Raised when the running request-body byte count exceeds the configured cap."""


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = 2_097_152) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers") or []:
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except (TypeError, ValueError):
                    declared = None
                if declared is not None and declared > self.max_bytes:
                    await self._reject(
                        send,
                        close_connection=str(scope.get("http_version") or "1.1") not in {"2", "3"},
                    )
                    return
                break

        received = 0
        response_started = False
        response_complete = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal response_started, response_complete
            message_type = message.get("type")
            if message_type == "http.response.start":
                response_started = True
            elif message_type == "http.response.body" and not message.get("more_body", False):
                response_complete = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await self._reject(
                    send,
                    close_connection=str(scope.get("http_version") or "1.1") not in {"2", "3"},
                )
            elif not response_complete:
                # ASGI headers cannot be changed after response.start. Complete the
                # started response rather than leaving the server waiting indefinitely.
                await send({
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                })

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
