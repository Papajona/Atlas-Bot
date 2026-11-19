"""BodySizeLimitMiddleware: pure-ASGI tests (no Starlette/FastAPI required)."""
import asyncio

from app.body_limit import BodySizeLimitMiddleware


def _run(coro):
    return asyncio.run(coro)


async def _drive(mw, *, headers=(), chunks=(b"",), http_version="1.1"):
    sent = []
    queue = list(chunks)

    async def receive():
        if queue:
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "http_version": http_version, "headers": list(headers), "path": "/x", "method": "POST"}
    await mw(scope, receive, send)
    return sent


def _reader_app():
    async def app(scope, receive, send):
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            total += len(message.get("body") or b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(total).encode()})
    return app


def test_declared_oversize_rejected_without_reading_body():
    mw = BodySizeLimitMiddleware(_reader_app(), max_bytes=10)
    sent = _run(_drive(mw, headers=[(b"content-length", b"11")], chunks=[b"x" * 11]))
    assert sent[0]["status"] == 413


def test_chunked_oversize_rejected_when_no_content_length():
    mw = BodySizeLimitMiddleware(_reader_app(), max_bytes=10)
    sent = _run(_drive(mw, chunks=[b"x" * 6, b"x" * 6]))
    assert sent[0]["status"] == 413


def test_within_limit_passes_through():
    mw = BodySizeLimitMiddleware(_reader_app(), max_bytes=10)
    sent = _run(_drive(mw, headers=[(b"content-length", b"10")], chunks=[b"x" * 10]))
    assert sent[0]["status"] == 200 and sent[1]["body"] == b"10"


def test_exact_boundary_allowed_and_zero_disables_limit():
    boundary = BodySizeLimitMiddleware(_reader_app(), max_bytes=10)
    boundary_result = _run(_drive(boundary, chunks=[b"x" * 10]))
    assert boundary_result[0]["status"] == 200

    disabled = BodySizeLimitMiddleware(_reader_app(), max_bytes=0)
    disabled_result = _run(_drive(disabled, chunks=[b"x" * 5000]))
    assert disabled_result[0]["status"] == 200


def test_non_http_scope_untouched():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    mw = BodySizeLimitMiddleware(app, max_bytes=1)
    _run(mw({"type": "websocket"}, None, None))
    assert called == ["websocket"]


def test_malformed_content_length_falls_back_to_streaming_count():
    mw = BodySizeLimitMiddleware(_reader_app(), max_bytes=10)
    sent = _run(_drive(mw, headers=[(b"content-length", b"abc")], chunks=[b"x" * 20]))
    assert sent[0]["status"] == 413


def test_oversize_after_response_start_completes_the_started_response():
    async def start_then_read(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        await receive()

    mw = BodySizeLimitMiddleware(start_then_read, max_bytes=10)
    sent = _run(_drive(mw, chunks=[b"x" * 6, b"x" * 6]))

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert sent[-1] == {
        "type": "http.response.body",
        "body": b"",
        "more_body": False,
    }


def test_http2_oversize_rejection_omits_connection_header():
    mw = BodySizeLimitMiddleware(_reader_app(), max_bytes=10)
    sent = _run(_drive(
        mw,
        headers=[(b"content-length", b"11")],
        chunks=[b"x" * 11],
        http_version="2",
    ))

    assert sent[0]["status"] == 413
    assert all(name != b"connection" for name, _value in sent[0]["headers"])
