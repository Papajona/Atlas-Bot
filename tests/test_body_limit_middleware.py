"""BodySizeLimitMiddleware: pure-ASGI tests (no Starlette/FastAPI required)."""
import asyncio

from app.body_limit import BodySizeLimitMiddleware


def _run(coro):
    return asyncio.run(coro)


async def _drive(mw, *, headers=(), chunks=(b"",), http_version="1.1", path="/x"):
    sent = []
    queue = list(chunks)

    async def receive():
        if queue:
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "http_version": http_version, "headers": list(headers), "path": path, "method": "POST"}
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


def test_chunked_oversize_is_rejected_before_route_can_start_a_400_response():
    called = []

    async def route_with_framework_parse_error(scope, receive, send):
        called.append(True)
        # Simulate a framework parser responding 400 to an interrupted body.
        await receive()
        await receive()
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": b"bad request"})

    mw = BodySizeLimitMiddleware(route_with_framework_parse_error, max_bytes=10)
    sent = _run(_drive(mw, chunks=[b"x" * 6, b"x" * 6]))

    assert sent[0]["status"] == 413
    assert called == []
    assert sent[-1]["body"] == b'{"detail":"Request body too large"}'


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



def test_chunked_body_at_limit_reaches_route():
    mw = BodySizeLimitMiddleware(_reader_app(), max_bytes=10)
    sent = _run(_drive(mw, chunks=[b"x" * 4, b"x" * 6]))
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"10"


def test_http3_oversize_rejection_omits_connection_header():
    mw = BodySizeLimitMiddleware(_reader_app(), max_bytes=10)
    sent = _run(_drive(
        mw,
        chunks=[b"x" * 11],
        http_version="3",
    ))
    assert sent[0]["status"] == 413
    assert all(name != b"connection" for name, _value in sent[0]["headers"])


def test_webhook_path_limit_is_applied_before_framework_body_buffering():
    called = []

    async def downstream(scope, receive, send):
        called.append(scope["path"])
        await _reader_app()(scope, receive, send)

    middleware = BodySizeLimitMiddleware(
        downstream,
        max_bytes=2_097_152,
        path_prefix_limits={"/api/webhooks/": 64},
    )
    sent = _run(_drive(
        middleware,
        chunks=[b"x" * 40, b"y" * 25],
        path="/api/webhooks/123",
    ))
    assert sent[0]["status"] == 413
    assert called == []


def test_path_specific_limit_does_not_change_other_routes():
    middleware = BodySizeLimitMiddleware(
        _reader_app(),
        max_bytes=100,
        path_prefix_limits={"/api/webhooks/": 10},
    )
    sent = _run(_drive(middleware, chunks=[b"x" * 11], path="/api/customer/profile"))
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"11"
