# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see ../NOTICE.
import functools as _functools

_registered_udfs = []


class Response:
    """An explicit HTTP response for a served route (plain code or a @fused.udf).

    Return this as `result` (or from a UDF) to control the body, content type,
    status, and headers. Any non-Response return value is sent as JSON. The
    marker attribute lets the runner detect a Response by identity — a plain
    dict/list/str can never be mistaken for one regardless of its contents.

        result = fused.Response("hi", media_type="text/plain")

    Body contract (validated here so mistakes fail at author time, not as an
    opaque 500 at request time):
      - media_type == "application/json": body may be any JSON-serializable value.
      - any other media_type: body must be str, bytes, or None. bytes are sent
        base64-encoded across the Lambda boundary and decoded by the serve handler
        (so binary responses — images, PDFs — work).

    `media_type` is required; use the helpers below for the common types.
    """

    __openfused_response__ = True

    def __init__(self, body, *, media_type, status_code=200, headers=None):
        if not isinstance(media_type, str) or not media_type:
            raise ValueError("media_type must be a non-empty string")
        if (
            not isinstance(status_code, int)
            or isinstance(status_code, bool)
            or not (100 <= status_code <= 599)
        ):
            raise ValueError("status_code must be an int in the range 100-599")
        if headers is not None and not isinstance(headers, dict):
            raise ValueError("headers must be a dict or None")
        if media_type != "application/json" and not isinstance(body, (str, bytes, type(None))):
            raise TypeError(
                "body for media_type " + repr(media_type) + " must be str, bytes, or None "
                "(got " + type(body).__name__ + "); use fused.JSONResponse for JSON data"
            )
        self.body = body
        self.media_type = media_type
        self.status_code = status_code
        self.headers = headers


class HTMLResponse(Response):
    def __init__(self, body, *, status_code=200, headers=None):
        super().__init__(body, media_type="text/html", status_code=status_code, headers=headers)


class PlainTextResponse(Response):
    def __init__(self, body, *, status_code=200, headers=None):
        super().__init__(body, media_type="text/plain", status_code=status_code, headers=headers)


class JSONResponse(Response):
    # Body stays a structured value; the serve handler emits it with JSONResponse.
    def __init__(self, data, *, status_code=200, headers=None):
        super().__init__(
            data, media_type="application/json", status_code=status_code, headers=headers
        )


class _UDF:
    # Marker for identity-based detection (mirrors Response.__openfused_response__):
    # the runner's explicit-entrypoint lookup unwraps a decorated name to its inner
    # function via this attribute plus `_fn` — never by duck-typed value checks.
    __openfused_udf__ = True

    def __init__(self, fn, filename=None, _register=True):
        # update_wrapper first: it does self.__dict__.update(fn.__dict__), which would
        # clobber _fn/_filename if fn carried those attrs (e.g. a stacked _UDF). Setting
        # them afterwards keeps our explicit assignments authoritative.
        _functools.update_wrapper(self, fn)
        self._fn = fn
        self._filename = filename
        if _register:
            _registered_udfs.append(self)

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)

    def map(self, items, filename=None, max_workers=16):
        # Fan out: invoke filename as a child Lambda for each item in items.
        # items: iterable of dicts; each dict becomes kwargs for one child invocation.
        # filename falls back to @fused.udf(filename=...) then "user_code.py".
        from concurrent.futures import ThreadPoolExecutor as _TPE  # noqa: N814

        try:
            from _openfused import invoke as _invoke  # type: ignore[import]
        except ImportError:
            raise RuntimeError("map() is only available inside Lambda execution")
        _filename = filename or self._filename or "user_code.py"

        def _call(kw):
            out = _invoke(_filename, **kw)
            return out["return_value"]

        _items = list(items)
        with _TPE(max_workers=max_workers) as _ex:
            _futures = [_ex.submit(_call, item) for item in _items]
        return [_f.result() for _f in _futures]


def udf(fn=None, *, filename=None):
    # Supports both @fused.udf and @fused.udf(filename="worker.py").
    # filename is the default target for udf.map(); falls back to "user_code.py".
    def _decorator(f):
        return _UDF(f, filename=filename)

    if fn is not None:
        return _UDF(fn, filename=None)
    return _decorator


def run(udf_fn, **kwargs):
    if callable(udf_fn):
        return udf_fn(**kwargs)
    raise TypeError("Expected a callable UDF, got " + str(type(udf_fn)))


def load(filename):
    # Returns a worker reference bound to filename.
    # A bare name is resolved to a .py file — fused.load("worker") loads
    # "worker.py" — so callers can drop the extension; an explicit ".py" is
    # left as-is. The file itself is not read here (no eager exec); it's read
    # inside _openfused.invoke() when the worker is called or .map()'d.
    # Calling it directly dispatches a single child Lambda invocation.
    # .map() fans out to multiple child Lambdas concurrently.
    _fname = filename if filename.endswith(".py") else filename + ".py"

    def _stub(**kwargs):
        try:
            from _openfused import invoke as _invoke
        except ImportError:
            raise RuntimeError(
                "fused.load() requires _openfused (only available inside Lambda execution)"
            )
        return _invoke(_fname, **kwargs)["return_value"]

    return _UDF(_stub, filename=_fname, _register=False)
