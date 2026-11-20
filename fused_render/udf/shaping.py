"""Shaping a Fused result into what a page gets (SPEC §54.4), stdlib only.

Used by `_worker.py`; kept apart so it can be read and tested without the
worker's side effects (it takes over stdout and imports the fused SDK).
"""
from __future__ import annotations

import json

AUTO_FORMAT = "png,html"
DATA_PAGE_MAX_ROWS = 1000


def text_of(value) -> str:
    return value if isinstance(value, str) else ""


def data_page(data) -> tuple[bytes, str]:
    """(body, content type) for a result that came back as a VALUE (a batch
    job's): a table as an HTML table, bytes as they are, the rest as text."""
    if data is None:
        return b"", ""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data), "application/octet-stream"
    if isinstance(data, str):
        return data.encode("utf-8"), "text/plain; charset=utf-8"
    to_html = getattr(data, "to_html", None)
    if callable(to_html) and hasattr(data, "columns"):
        frame = data
        to_wkt = getattr(frame, "to_wkt", None)
        if callable(to_wkt):  # geometry as text, as Fused's own table page shows it
            frame = to_wkt()
        total = len(frame)
        html = frame.head(DATA_PAGE_MAX_ROWS).to_html(border=0)
        note = (f"<p>Showing the first {DATA_PAGE_MAX_ROWS} of {total} rows.</p>"
                if total > DATA_PAGE_MAX_ROWS else "")
        page = ("<html><head><meta charset='utf-8'><style>"
                "body{font:13px/1.4 -apple-system,'Segoe UI',Roboto,sans-serif;margin:12px}"
                "table{border-collapse:collapse}th,td{padding:4px 10px;text-align:left;"
                "border-bottom:1px solid #ddd;white-space:nowrap}th{position:sticky;top:0;"
                "background:#fff}</style></head><body>" + note + html + "</body></html>")
        return page.encode("utf-8"), "text/html; charset=utf-8"
    if isinstance(data, (dict, list, int, float, bool)):
        try:
            return json.dumps(data, default=str).encode("utf-8"), "application/json"
        except (TypeError, ValueError):
            pass
    return repr(data).encode("utf-8"), "text/plain; charset=utf-8"


# The page's format names -> what the SDK is asked for. `auto`, `html` and
# `png` are Fused's own renderings (the ones a shared UDF's link serves).
# `parquet` and `json` ask for Fused's default serialization (Parquet for a
# table, JSON for anything else): the SDK cannot hand back a table as JSON or
# CSV with `_return_response` (it parses every table answer as Parquet), so
# `json` is made here from the value.
SDK_FORMATS = {"auto": AUTO_FORMAT, "html": "html", "png": "png", "parquet": None, "json": None,
               "view": None}
# `view`: the value in the shape a page draws it in, with no page of Fused's
# around it (SPEC §54.2): a table as TABLE_TYPE JSON, an HTML string as HTML,
# other text as text, a raster as PNG (asked of Fused separately: NEEDS_PNG),
# anything else as JSON.
TABLE_TYPE = "application/vnd.fused-render.table+json"
# Rows of a table a `view` carries; `total` says how many there were.
VIEW_MAX_ROWS = 20_000
PARQUET_TYPE = "application/vnd.apache.parquet"


def _header_type(result) -> str:
    headers = getattr(result, "original_headers", None) or {}
    for key in ("content-type", "Content-Type"):
        try:
            value = headers.get(key)
        except Exception:  # noqa: BLE001 — headers of an unexpected shape
            value = None
        if value:
            return str(value)
    return ""


_WKB_NAMES = {1: "POINT", 2: "LINESTRING", 3: "POLYGON", 4: "MULTIPOINT",
              5: "MULTILINESTRING", 6: "MULTIPOLYGON", 7: "GEOMETRYCOLLECTION"}


def wkb_to_wkt(raw: bytes) -> str:
    """WKT of a WKB geometry (ISO or EWKB, 2D/Z/M/ZM), stdlib only — the
    bundle ships no shapely. Raises ValueError on anything else."""
    import struct

    pos = 0

    def read(fmt: str, order: str):
        nonlocal pos
        size = struct.calcsize(order + fmt)
        if pos + size > len(raw):
            raise ValueError("truncated WKB")
        out = struct.unpack_from(order + fmt, raw, pos)
        pos += size
        return out

    def num(v: float) -> str:
        return repr(v) if v != int(v) or abs(v) > 1e15 else str(int(v))

    def geom() -> str:
        (flag,) = read("B", "<")
        if flag not in (0, 1):
            raise ValueError("not WKB")
        order = "<" if flag else ">"
        (code,) = read("I", order)
        has_z = bool(code & 0x80000000)
        has_m = bool(code & 0x40000000)
        if code & 0x20000000:  # EWKB SRID
            read("I", order)
        code &= 0x0FFFFFFF
        if code > 1000:
            dims, code = code // 1000, code % 1000
            has_z, has_m = dims in (1, 3), dims in (2, 3)
        name = _WKB_NAMES.get(code)
        if name is None:
            raise ValueError("unknown WKB type")
        width = 2 + has_z + has_m

        def coords(n: int) -> str:
            return ", ".join(" ".join(num(v) for v in read("d" * width, order)) for _ in range(n))

        def ring() -> str:
            (n,) = read("I", order)
            return "(" + coords(n) + ")"

        if code == 1:
            values = read("d" * width, order)
            if all(v != v for v in values):
                return "POINT EMPTY"
            return "POINT (" + " ".join(num(v) for v in values) + ")"
        if code == 2:
            (n,) = read("I", order)
            return f"LINESTRING ({coords(n)})" if n else "LINESTRING EMPTY"
        if code == 3:
            (n,) = read("I", order)
            return f"POLYGON ({', '.join(ring() for _ in range(n))})" if n else "POLYGON EMPTY"
        (n,) = read("I", order)
        parts = [geom() for _ in range(n)]
        if not parts:
            return f"{name} EMPTY"
        if code == 7:
            return f"{name} ({', '.join(parts)})"
        inner = [p[p.index("("):] if "(" in p else "EMPTY" for p in parts]
        return f"{name} ({', '.join(inner)})"

    text = geom()
    if pos != len(raw):
        raise ValueError("trailing bytes")
    return text


def _plain(value):
    """One cell as something json can write: bytes as WKT when they are a
    geometry (a table's WKB column), else base64; numpy scalars as Python
    ones; NaN/NaT as null; times as ISO text; anything else as text."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return wkb_to_wkt(raw)
        except Exception:  # noqa: BLE001 — not a geometry
            import base64

            return base64.b64encode(raw).decode("ascii")
    item = getattr(value, "item", None)
    if callable(item) and not isinstance(value, (list, dict, str)):
        try:
            value = item()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, float) and value != value:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return None if str(value) == "NaT" else iso()
        except Exception:  # noqa: BLE001
            pass
    wkt = getattr(value, "wkt", None)
    if isinstance(wkt, str):
        return wkt
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)


def json_body(data) -> bytes:
    """The value as JSON: a table as a list of row objects (geometry as WKT,
    as Fused's own table page shows it), anything else as itself."""
    if hasattr(data, "columns") and callable(getattr(data, "to_dict", None)):
        rows = data.to_dict(orient="records")
        data = [{str(k): _plain(v) for k, v in row.items()} for row in rows]
        return json.dumps(data, allow_nan=False).encode("utf-8")
    return json.dumps(_plain(data), allow_nan=False).encode("utf-8")


class NeedsPng(Exception):
    """A `view` of a raster: the worker asks Fused for its PNG rendering."""


def table_body(frame) -> bytes:
    """A table as `{columns, rows, total}` (rows as lists, at most
    VIEW_MAX_ROWS of them; geometry as WKT, as `json_body` writes cells)."""
    total = len(frame)
    to_wkt = getattr(frame, "to_wkt", None)
    if callable(to_wkt):
        frame = to_wkt()
    split = frame.head(VIEW_MAX_ROWS).to_dict(orient="split")
    columns = [str(c) for c in split.get("columns", [])]
    rows = [[_plain(v) for v in row] for row in split.get("data", [])]
    return json.dumps({"columns": columns, "rows": rows, "total": total},
                      allow_nan=False).encode("utf-8")


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith("<") and any(t in head for t in (
        "<html", "<!doctype", "<div", "<body", "<head", "<svg", "<table", "<style", "<script",
        "<p", "<h1", "<h2", "<iframe", "<canvas"))


def view_body(result) -> tuple[bytes, str]:
    """The `view` shape of a successful result (see TABLE_TYPE). Raises
    NeedsPng for a raster (an array, an image)."""
    content = getattr(result, "original_content", None) or b""
    data = getattr(result, "data", None)
    if data is None:
        return content, _header_type(result) or ("application/json" if content else "")
    if hasattr(data, "columns") and callable(getattr(data, "to_dict", None)):
        return table_body(data), TABLE_TYPE
    if isinstance(data, str):
        if _looks_like_html(data):
            return data.encode("utf-8"), "text/html; charset=utf-8"
        return data.encode("utf-8"), "text/plain; charset=utf-8"
    if isinstance(data, (bytes, bytearray)):
        return bytes(data), "application/octet-stream"
    if hasattr(data, "shape") or (hasattr(data, "save") and hasattr(data, "mode")):
        raise NeedsPng()
    return json_body(data), "application/json"


def body_of(result, fmt: str) -> tuple[bytes, str]:
    """(body, content type) of a successful result in the page's format."""
    if fmt == "view":
        return view_body(result)
    content = getattr(result, "original_content", None) or b""
    data = getattr(result, "data", None)
    if fmt == "json":
        if data is None and content:
            return content, "application/json"
        return json_body(data), "application/json"
    if fmt == "parquet":
        if content:
            kind = PARQUET_TYPE if content[:4] == b"PAR1" else (_header_type(result) or "application/json")
            return content, kind
        return data_page(data)
    if not content and data is not None:
        return data_page(data)  # a batch job comes back as a value
    return content, _header_type(result)
