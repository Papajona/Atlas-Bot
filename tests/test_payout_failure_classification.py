"""Payout failures must default to UNKNOWN unless rejection is provable."""
import ast
import pathlib


def _load():
    try:
        from app.payout import classify_payout_exception, PayoutError, PayoutUnknown
        return classify_payout_exception, PayoutError, PayoutUnknown
    except Exception:
        src = pathlib.Path(__file__).resolve().parents[1].joinpath("app", "payout.py").read_text()
        tree = ast.parse(src)
        keep = []
        for n in tree.body:
            if isinstance(n, ast.ClassDef) and n.name in {"PayoutError", "PayoutUnknown"}:
                keep.append(n)
            elif isinstance(n, ast.FunctionDef) and n.name == "classify_payout_exception":
                keep.append(n)
            elif isinstance(n, ast.Assign) and any(getattr(t, "id", "") in {"_DEFINITE_HTTP_REJECTIONS", "_DEFINITE_CCXT_REJECTIONS"} for t in n.targets):
                keep.append(n)
        ns = {}
        exec(compile(ast.Module(keep, []), "payout.py", "exec"), ns)
        return ns["classify_payout_exception"], ns["PayoutError"], ns["PayoutUnknown"]


classify, PayoutError, PayoutUnknown = _load()


class _Resp:
    def __init__(self, code):
        self.status_code = code


class _HTTPErr(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.response = _Resp(code)


def test_definite_rejections_are_failures():
    for code in (400, 401, 403, 404, 422):
        e = classify(_HTTPErr(code))
        assert isinstance(e, PayoutError) and not isinstance(e, PayoutUnknown)


def test_ambiguous_http_outcomes_are_unknown():
    for code in (408, 409, 425, 429, 500, 502, 503, 504):
        assert isinstance(classify(_HTTPErr(code)), PayoutUnknown)


def test_malformed_or_protocol_errors_are_unknown():
    assert isinstance(classify(ValueError("malformed response")), PayoutUnknown)
    assert isinstance(classify(RuntimeError("peer closed connection without response")), PayoutUnknown)


def test_ccxt_style_names():
    class InsufficientFunds(Exception):
        pass

    class ExchangeError(Exception):
        pass

    class RequestTimeout(ExchangeError):
        pass

    assert isinstance(classify(InsufficientFunds("x")), PayoutError)
    assert isinstance(classify(ExchangeError("x")), PayoutUnknown)
    assert isinstance(classify(RequestTimeout("x")), PayoutUnknown)


def test_existing_payout_errors_pass_through():
    u = PayoutUnknown("x")
    f = PayoutError("y")
    assert classify(u) is u and classify(f) is f
