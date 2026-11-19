"""Pure-ASGI request body size limit.

Reject oversized request bodies before framework parsers and application routes
can start a response. Both declared Content-Length and streamed/chunked bodies
are bounded. At most max_bytes of request payload is buffered.
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

        # Reject any oversized declared length before reading the payload. The
        # streamed byte count below remains authoritative for absent, malformed,
        # duplicate, or understated Content-Length headers.
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

        # Read and validate before calling downstream middleware. Raising from
        # receive() while a framework parser is active can be converted into an
        # HTTP 400/500 after headers have started, making a true 413 impossible.
        buffered = bytearray()
        saw_request = False
        disconnected = False
        extension_message = None
        request_complete = False

        while True:
            message = await receive()
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
