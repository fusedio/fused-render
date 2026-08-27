"""Compile a local fused-render app into an ordinary Fused Canvas directory.

The output deliberately uses only existing Workbench primitives: ``canvas.toml``
and ``@fused.udf`` sources.  No application-server deployment endpoint or
``openfused_server`` path is involved.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import zipfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from fused_render.export import (
    ExportError,
    ExportPlan,
    _asset_key,
    _validate_cache_max_age,
    plan_export,
)


MAX_GENERATED_FILE_BYTES = 4_000_000
MAX_GENERATED_TOTAL_BYTES = 12_000_000
_SLUG_CHARS = re.compile(r"[^A-Za-z0-9_]+")
_CANVAS_NAME = re.compile(r"^[A-Za-z0-9_]{1,128}$")
_CACHE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True)
class CompiledWorkbenchApp:
    canvas_name: str
    digest: str
    shell_slug: str
    files: dict[str, bytes]
    entrypoints: dict[str, str]
    assets: dict[str, str]
    warnings: tuple[str, ...]

    @property
    def generated_bytes(self) -> int:
        return sum(len(value) for value in self.files.values())


def _udf_slug(prefix: str, digest: str, value: str = "") -> str:
    """A stable, unique UDF name for one generated route.

    The readable tail is truncated to fit the 96-char budget, so two long route
    names that agree on their first bytes would otherwise land on one slug —
    and one generated .py would silently overwrite the other, leaving both
    entrypoints pointing at whichever file was written last.  A hash of the
    FULL value goes in ahead of the truncated tail so the slug stays unique
    however much of the tail survives.
    """
    slug = f"fr_{prefix}_{digest[:8]}"
    if value:
        mark = hashlib.blake2b(value.encode("utf-8"), digest_size=4).hexdigest()
        tail = _SLUG_CHARS.sub("_", value).strip("_")
        slug += "_" + mark + ("_" + tail if tail else "")
    return slug[:96].rstrip("_")


def _cache_seconds(value: str) -> int:
    _validate_cache_max_age(value)
    amount = int(value[:-1])
    return amount * _CACHE_UNITS[value[-1]]


def _runtime_js() -> str:
    return (
        resources.files("fused_render") / "static" / "workbench_runtime.js"
    ).read_text(encoding="utf-8")


def _script_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).replace("<", "\\u003c")


def _inject_runtime(html: str, seed: dict) -> str:
    injection = (
        "<script>window.__FUSED_RENDER_WORKBENCH__="
        + _script_json(seed)
        + ";</script><script>"
        + _runtime_js()
        + "</script>"
    )
    # The local server injects immediately AFTER the opening <head>
    # (server/routers/render.py), and this has to land in the same place: at
    # the END of <head> instead, a page whose own <head> script calls
    # fused.runPython works locally and breaks once deployed. The no-<head>
    # fallback is local's too — prepending ahead of the doctype, which costs
    # both of them standards mode; worth fixing, but worth fixing in both at
    # once, so the two stay honest about being one contract.
    index = html.lower().find("<head>")
    if index < 0:
        return injection + html
    insert_at = index + len("<head>")
    return html[:insert_at] + injection + html[insert_at:]


def _deterministic_archive(page_dir: str, plan: ExportPlan) -> bytes:
    """Archive the runtime tree used by every generated runPython wrapper."""
    keys: set[str] = set()
    for item in plan.entrypoints:
        keys.add(_asset_key(item.path))
    for item in plan.resources:
        keys.add(item.key)
    # Assets also belong in the Python cwd so open("data.csv") matches local execution.
    for item in plan.assets:
        keys.add(item.name)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for key in sorted(keys):
            source = os.path.join(page_dir, *key.split("/"))
            info = zipfile.ZipInfo(key, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(source, "rb") as fh:
                zf.writestr(info, fh.read())
    return out.getvalue()


# job2's serializer (job2/serialize/udf.py::_serialize_output) is the whole
# contract for what a Workbench UDF may return.  Only two branches matter here:
# a `str` return is serialized as text/html, and a `fastapi.Response` is passed
# through as its own body + media_type.  `fused.HTMLResponse` and friends exist
# only in the openfused serve-plane shim (agent_core/backends/aws/handler),
# which is exactly the plane this deployment path bypasses.
def _shell_source(html: str) -> str:
    return """\
import fused

_HTML = %s

@fused.udf
def udf():
    # A str return is served as text/html; charset=utf-8.
    return _HTML
""" % ascii(html)


def _run_source(target: str, archive: bytes, digest: str) -> str:
    payload = base64.b64encode(archive).decode("ascii")
    return f'''\
import base64 as _fr_base64
import inspect as _fr_inspect
import io as _fr_io
import json as _fr_json_mod
import os as _fr_os
import runpy as _fr_runpy
import shutil as _fr_shutil
import sys as _fr_sys
import tempfile as _fr_tempfile
import zipfile as _fr_zipfile
import fused

_FR_ARCHIVE = {payload!r}
_FR_DIGEST = {digest!r}
_FR_TARGET = {target!r}


def _fr_json(value):
    """Hand a result back as application/json.

    job2 serializes a bare dict as a multi-part zip and a bare str as HTML, so
    returning the value directly would not match the page's fetch().json() —
    nor the local bridge, which JSON-encodes every return value
    (fused_render/engine.py).  A fastapi Response is job2's one pass-through
    branch: it forwards body + media_type verbatim.
    """
    from fastapi import Response as _FrResponse

    body = _fr_json_mod.dumps(value, default=str).encode("utf-8")
    return _FrResponse(body, media_type="application/json")


def _fr_coerce(value, annotation):
    if annotation is _fr_inspect.Parameter.empty:
        return value
    try:
        if annotation is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if annotation in (int, float, str) and not isinstance(value, annotation):
            return annotation(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("could not convert param to " + annotation.__name__ + ": " + str(exc))
    return value


def _fr_bind(fn, params):
    try:
        signature = _fr_inspect.signature(fn, eval_str=True)
    except Exception:
        signature = _fr_inspect.signature(fn)
    accepts_kwargs = any(
        param.kind is _fr_inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )
    kwargs = {{}}
    for name, param in signature.parameters.items():
        if param.kind in (_fr_inspect.Parameter.VAR_KEYWORD, _fr_inspect.Parameter.VAR_POSITIONAL):
            continue
        if name in params:
            kwargs[name] = _fr_coerce(params[name], param.annotation)
        elif param.default is _fr_inspect.Parameter.empty:
            raise TypeError("missing required param: " + repr(name))
    if accepts_kwargs:
        for key, value in params.items():
            if key not in kwargs:
                kwargs[key] = value
    return kwargs


def _fr_materialize():
    """Publish the app tree at a digest-keyed path, atomically.

    Every route of one app shares this digest and this archive, and each route
    is its own UDF hence its own process, so the first load of an app with two
    Python routes has them racing to populate the same directory.  Extracting
    in place lost that race badly: extractall reopens each member "wb", so a
    second extractor would truncate and refill a module a first reader was
    already importing — an intermittent SyntaxError on the first load after a
    deploy, self-healing and therefore unreproducible.

    So nothing is ever written into the published path.  Extraction happens in
    a private staging directory that is renamed into place whole; losing the
    rename race is fine, because every copy of a digest-keyed tree is
    byte-identical.  The directory's existence is the completion signal, which
    is why there is no marker file any more.
    """
    parent = _fr_os.path.join(_fr_tempfile.gettempdir(), "fused-render-workbench")
    root = _fr_os.path.join(parent, _FR_DIGEST)
    if _fr_os.path.isdir(root):
        return root
    _fr_os.makedirs(parent, exist_ok=True)
    staging = _fr_tempfile.mkdtemp(prefix=_FR_DIGEST + ".", dir=parent)
    try:
        with _fr_zipfile.ZipFile(
            _fr_io.BytesIO(_fr_base64.b64decode(_FR_ARCHIVE)), "r"
        ) as archive:
            archive.extractall(staging)
        # Same filesystem, so this is atomic: readers see the tree complete or
        # not at all. It fails when another process got there first, which is
        # not an error — their tree is ours.
        _fr_os.rename(staging, root)
    except OSError:
        if not _fr_os.path.isdir(root):
            raise
    finally:
        _fr_shutil.rmtree(staging, ignore_errors=True)
    return root


@fused.udf
def udf(**params):
    root = _fr_materialize()
    target = _fr_os.path.join(root, *_FR_TARGET.split("/"))
    old_cwd = _fr_os.getcwd()
    old_path = list(_fr_sys.path)
    # Bring our own registry rather than reading fused's.  The app file's
    # decorated function may be named anything (locally, engine.py takes
    # _registered_udfs[-1] and calls ._fn, never looking at the name), but
    # _registered_udfs exists only in the openfused serve-plane shim — on the
    # real wheel @fused.udf just returns a Udf object that is recorded
    # nowhere, so an app holding one @fused.udf and no main() used to raise.
    # Swapping in a capturing identity decorator for the duration of the load
    # gives us the raw function with its module globals intact, which is what
    # local calls too; Udf.run_local is not the tool, since a Udf built this
    # way carries only the decorated function's own source.
    captured = []
    real_udf = getattr(fused, "udf", None)

    def _fr_capture(fn):
        captured.append(fn)
        return fn

    try:
        _fr_os.chdir(root)
        if root not in _fr_sys.path:
            _fr_sys.path.insert(0, root)
        if real_udf is not None:
            fused.udf = _fr_capture
        namespace = _fr_runpy.run_path(target, run_name="__fused_render__")
        if "result" in namespace:
            return _fr_json(namespace["result"])
        if captured:
            inner = captured[-1]
            return _fr_json(inner(**_fr_bind(inner, params)))
        main = namespace.get("main")
        if not callable(main):
            raise AttributeError(
                "fused-render app code must define main(), one @fused.udf, or result"
            )
        return _fr_json(main(**_fr_bind(main, params)))
    finally:
        if real_udf is not None:
            fused.udf = real_udf
        _fr_sys.path[:] = old_path
        _fr_os.chdir(old_cwd)
'''


def _asset_source(assets: dict[str, bytes]) -> str:
    encoded = {
        key: base64.b64encode(value).decode("ascii")
        for key, value in sorted(assets.items())
    }
    media = {key: mimetypes.guess_type(key)[0] or "application/octet-stream" for key in assets}
    return f'''\
import base64 as _fr_base64
import fused

_FR_ASSETS = {encoded!r}
_FR_MEDIA = {media!r}

@fused.udf
def udf(name: str = ""):
    # job2 forwards a fastapi Response's body and media_type and nothing else:
    # status_code and headers are dropped, so a missing asset comes back as a
    # 200 with this body rather than a 404, and Cache-Control cannot be set
    # here (the plane's own cache_max_age query param is what the runtime uses).
    from fastapi import Response as _FrResponse

    if name not in _FR_ASSETS:
        return _FrResponse(b"asset not found", media_type="text/plain")
    return _FrResponse(
        _fr_base64.b64decode(_FR_ASSETS[name]),
        media_type=_FR_MEDIA[name],
    )
'''


def _canvas_toml(name: str, shell: str, helpers: list[str]) -> str:
    lines = [
        'type = "canvas"',
        "version = 2",
        "name = " + json.dumps(name),
        "",
        "[canvas]",
        "edges = []",
        "",
        "[[canvas.nodes]]",
        "udfName = " + json.dumps(shell),
        'title = "App"',
        "x = 0.0",
        "y = 0.0",
        "zIndex = 1",
        "width = 1000.0",
        "height = 700.0",
    ]
    for index, slug in enumerate(helpers, start=2):
        lines += [
            "",
            "[[canvas.nodes]]",
            "udfName = " + json.dumps(slug),
            "x = 0.0",
            f"y = {800.0 + index}",
            f"zIndex = {index}",
            "width = 1.0",
            "height = 1.0",
            "visible = false",
        ]
    return "\n".join(lines) + "\n"


def compile_workbench_app(
    html_path: str,
    canvas_name: str,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    cache_max_age: str = "0s",
) -> CompiledWorkbenchApp:
    """Compile one app entry page without writing files or contacting Fused."""
    if not isinstance(canvas_name, str) or not _CANVAS_NAME.fullmatch(canvas_name.strip()):
        raise ExportError(
            "canvas_name must contain 1-128 letters, numbers, or underscores"
        )
    _validate_cache_max_age(cache_max_age)
    path = os.path.abspath(html_path)
    if not os.path.isfile(path) or Path(path).suffix.lower() not in (".html", ".htm"):
        raise ExportError(f"no such .html/.htm app entry: {path}")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        html = fh.read()
    page_dir = os.path.dirname(path)
    plan = plan_export(html, page_dir, include=include, exclude=exclude)
    if plan.errors:
        raise ExportError("cannot deploy app:\n  - " + "\n  - ".join(plan.errors))

    archive = _deterministic_archive(page_dir, plan)
    digest_input = html.encode("utf-8") + b"\0" + archive + b"\0" + cache_max_age.encode()
    digest = hashlib.sha256(digest_input).hexdigest()
    shell_slug = _udf_slug("shell", digest)
    entrypoints = {
        item.path: _udf_slug("run", digest, item.name) for item in plan.entrypoints
    }
    asset_slug = _udf_slug("asset", digest) if plan.assets else None
    assets_map = {item.path: item.name for item in plan.assets}
    seed = {
        "shell": shell_slug,
        "entrypoints": entrypoints,
        "assets": assets_map,
        "assetRoute": asset_slug,
        "cacheMaxAge": _cache_seconds(cache_max_age),
    }
    hosted_html = _inject_runtime(html, seed)

    text_files: dict[str, str] = {f"{shell_slug}.py": _shell_source(hosted_html)}
    for item in plan.entrypoints:
        target = _asset_key(item.path)
        text_files[f"{entrypoints[item.path]}.py"] = _run_source(target, archive, digest)
    if asset_slug:
        raw_assets: dict[str, bytes] = {}
        for item in plan.assets:
            with open(os.path.join(page_dir, item.path), "rb") as fh:
                raw_assets.setdefault(item.name, fh.read())
        text_files[f"{asset_slug}.py"] = _asset_source(raw_assets)
    helpers = [*entrypoints.values(), *([asset_slug] if asset_slug else [])]
    text_files["canvas.toml"] = _canvas_toml(canvas_name.strip(), shell_slug, helpers)
    files = {name: value.encode("utf-8") for name, value in text_files.items()}
    oversized = [
        (name, len(value))
        for name, value in files.items()
        if len(value) > MAX_GENERATED_FILE_BYTES
    ]
    total = sum(len(value) for value in files.values())
    if oversized or total > MAX_GENERATED_TOTAL_BYTES:
        details = ", ".join(f"{name} ({size:,} bytes)" for name, size in oversized)
        if total > MAX_GENERATED_TOTAL_BYTES:
            details += (", " if details else "") + f"total ({total:,} bytes)"
        raise ExportError(
            "generated Canvas exceeds the conservative source-size limit: " + details +
            ". Reduce embedded assets or the number of runPython entrypoints."
        )
    warnings = list(plan.warnings)
    if plan.assets:
        warnings.append(
            "Hosted assets support full responses only; HTTP Range requests are not supported."
        )
    if plan.entrypoints:
        warnings += [
            "Public Canvas sharing exposes the generated UDF sources, including embedded "
            "Python app code and resources.",
            "Python dependencies must already be available in the Workbench execution "
            "environment; project dependency files are not installed by this deployment.",
        ]
    warnings.append(
        "The generated Canvas is dedicated to this app; a deploy replaces its remote UDF list."
    )
    return CompiledWorkbenchApp(
        canvas_name=canvas_name.strip(),
        digest=digest,
        shell_slug=shell_slug,
        files=files,
        entrypoints=entrypoints,
        assets=assets_map,
        warnings=tuple(warnings),
    )


def write_compiled_canvas(compiled: CompiledWorkbenchApp, directory: str) -> None:
    """Realize a compiled Canvas in an empty directory."""
    os.makedirs(directory, exist_ok=True)
    if os.listdir(directory):
        raise ExportError(f"generated Canvas directory must be empty: {directory}")
    for name, content in compiled.files.items():
        with open(os.path.join(directory, name), "wb") as fh:
            fh.write(content)
