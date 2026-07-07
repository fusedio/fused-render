"""Export a renderable HTML page into a portable bundle for hosted serving.

fused-render is local-only by design (SPEC §1 "Never deployed to cloud"): the
server binds 127.0.0.1 and hosts nothing. This module does **not** change that.
It adds a pure, offline *build* step — ``fused-render export`` — that statically
collects a page's dependencies into a self-contained bundle directory. A separate
hosting layer (the ``fused`` wheel's ``build_html_artifact``) turns that bundle
into a served app; nothing here opens a socket, uploads, or phones home.

Only the **portable subset** of the injected ``window.fused`` API is supported on
a hosted page, because a served page has no local filesystem behind it:

  * ``fused.runPython(pyPath, params)`` — supported. The referenced ``.py`` file
    is bundled and, when hosted, becomes a served entrypoint the page POSTs to.
  * ``fused.rawUrl(path)`` — supported. The referenced file is bundled as a
    read-only asset served by an ``_asset`` route.
  * ``fused.readFile(path)`` — supported (same bundling as ``rawUrl``).
  * ``fused.params`` — supported unchanged (pure client-side URL state).
  * ``fused.writeFile`` / ``fused.stat`` / SSE live-reload — **unsupported**: a
    hosted artifact is immutable and has no filesystem to stat. Their use in an
    exported page is reported as an error, not silently dropped.

Every path argument to ``runPython`` / ``rawUrl`` / ``readFile`` must be a **string
literal**. A dynamically-computed path (a variable, a template string) cannot be
resolved at build time, so the export fails loudly rather than shipping a page
whose data calls 404 at request time.

Known limitation: dependency scanning is a regex over the whole HTML, so a
``fused.runPython("…")``-shaped snippet sitting inside a JS string literal or an
HTML comment (never actually executed) is still treated as a real call. The
consequence is only a **loud** export error (a spurious missing-file or
non-literal-path failure) or a harmlessly-bundled extra file — never silent wrong
behavior at request time. Robustly excluding commented/quoted occurrences would
require a full JS tokenizer, which is disproportionate here; author pages so that
such look-alike text is not present, or split it out.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field

# Route names the hosting layer reserves (serve control paths + the shell/asset
# routes build_html_artifact mints). A run entrypoint must never collide with one,
# and no exported name may start with "_" (that namespace is host-internal).
_RESERVED_NAMES = frozenset(
    {
        "health",
        "data",
        "_shell",
        "_asset",
        "_fetch",
        "_query",
        "_authinfo",
        "_callback",
    }
)

# A `fused.<method>(` call whose first argument is a single- or double-quoted
# string literal. Group 2 is the literal's contents. Calls that don't match this
# (first arg is a variable/expression) are caught separately as dynamic-path errors.
_LITERAL_CALL = {
    method: re.compile(r"fused\.%s\(\s*(['\"])(.*?)\1" % method)
    for method in ("runPython", "rawUrl", "readFile")
}
# Any `fused.<method>(` occurrence, literal or not — used to detect dynamic paths
# (an occurrence not covered by the literal match above).
_ANY_CALL = {method: re.compile(r"fused\.%s\(" % method) for method in _LITERAL_CALL}

# Unsupported API surface: present in an exported page => hard error.
_UNSUPPORTED = re.compile(r"fused\.(writeFile|stat)\(")


class ExportError(Exception):
    """A user-correctable failure while exporting a page (CLI prints it verbatim)."""


@dataclass(frozen=True)
class Entrypoint:
    """A ``runPython`` target: the literal path in the page, its bundled file, and the
    served route name the hosting layer will expose (what the page POSTs to)."""

    path: str  # the literal string passed to runPython, e.g. "./sine.py"
    name: str  # the served route name, e.g. "sine"
    file: str  # bundle-relative destination, e.g. "code/sine.py"


@dataclass(frozen=True)
class Asset:
    """A ``rawUrl``/``readFile`` target: a read-only file bundled and served by ``_asset``."""

    path: str  # the literal string passed to rawUrl/readFile, e.g. "./logo.png"
    name: str  # the asset key the page requests, e.g. "logo.png"
    file: str  # bundle-relative destination, e.g. "assets/logo.png"


@dataclass
class ExportPlan:
    """What an export will bundle, plus any blocking problems found while scanning."""

    entrypoints: list[Entrypoint] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _slugify(stem: str) -> str:
    """Lowercase, mapping runs of non-``[a-z0-9]`` to single hyphens: ``My_File`` → ``my-file``."""
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def _route_name(rel_path: str, taken: set[str]) -> str:
    """A unique, valid, non-reserved route name derived from a ``.py`` path.

    Slugifies the filename stem; falls back to ``run`` when nothing survives, prefixes
    ``run-`` when the slug is reserved or host-internal (leading ``_``), and appends
    ``-2``, ``-3``, … on collision so two files with the same stem stay distinct.
    """
    base = _slugify(os.path.splitext(os.path.basename(rel_path))[0]) or "run"
    if base in _RESERVED_NAMES or base.startswith("-"):
        base = f"run-{base.lstrip('-')}"
    name = base
    n = 2
    while name in taken:
        name = f"{base}-{n}"
        n += 1
    taken.add(name)
    return name


def _literal_paths(html: str, method: str) -> list[str]:
    """Ordered, de-duplicated literal path arguments to ``fused.<method>(`` in ``html``."""
    seen: dict[str, None] = {}
    for m in _LITERAL_CALL[method].finditer(html):
        seen.setdefault(m.group(2), None)
    return list(seen)


def _dynamic_call_count(html: str, method: str) -> int:
    """How many ``fused.<method>(`` calls do NOT have a leading string literal.

    Total occurrences minus literal-first-arg matches — anything left is a call whose
    path is computed at runtime, which cannot be resolved into a bundle.
    """
    total = len(_ANY_CALL[method].findall(html))
    literal = len(_LITERAL_CALL[method].findall(html))
    return total - literal


def _reject_unsafe_rel(path: str, kind: str, errors: list[str]) -> bool:
    """Reject an absolute path or one escaping the page directory (``..``). Returns True if OK."""
    if os.path.isabs(path):
        errors.append(
            f"{kind} path {path!r} is absolute; hosted bundles only support paths relative "
            "to the page (a hosted page has no filesystem to reach outside its bundle)"
        )
        return False
    normalized = os.path.normpath(path)
    if normalized.startswith(".."):
        errors.append(
            f"{kind} path {path!r} escapes the page directory; only files beside the page "
            "(or below it) can be bundled"
        )
        return False
    return True


def _within_page_dir(page_dir: str, target: str) -> bool:
    """True iff ``target``'s **real** path stays inside ``page_dir``.

    ``_reject_unsafe_rel`` blocks lexical escapes (``..``, absolute), but a symlink that
    lexically stays under the page can still point outside the tree — and ``copyfile``
    would follow it into the bundle. Compare resolved real paths so such a symlink is
    caught before it is bundled.
    """
    root = os.path.realpath(page_dir)
    real = os.path.realpath(target)
    return real == root or real.startswith(root + os.sep)


def plan_export(html: str, page_dir: str) -> ExportPlan:
    """Scan a page's HTML and build an :class:`ExportPlan` (pure — no files written).

    Resolves each literal ``runPython``/``rawUrl``/``readFile`` path against ``page_dir``,
    recording blocking problems (dynamic paths, unsupported API, unsafe/missing files) in
    ``plan.errors`` rather than raising — so a caller can report them all at once.
    """
    plan = ExportPlan()

    for m in _UNSUPPORTED.finditer(html):
        api = m.group(1)
        plan.errors.append(
            f"fused.{api}() is not supported on a hosted page (a served artifact is "
            "immutable and has no filesystem); remove it before exporting"
        )

    for method in ("runPython", "rawUrl", "readFile"):
        dyn = _dynamic_call_count(html, method)
        if dyn > 0:
            plan.errors.append(
                f"{dyn} fused.{method}() call(s) use a non-literal (computed) path; the "
                "exporter can only bundle string-literal paths known at build time"
            )

    taken_names: set[str] = set()
    for path in _literal_paths(html, "runPython"):
        if not _reject_unsafe_rel(path, "runPython", plan.errors):
            continue
        src = os.path.join(page_dir, path)
        if not _within_page_dir(page_dir, src):
            plan.errors.append(
                f"runPython target {path!r} resolves outside the page directory "
                "(a symlink escaping the bundle); only files under the page can be bundled"
            )
            continue
        if not os.path.isfile(src):
            plan.errors.append(f"runPython target {path!r} not found next to the page ({src})")
            continue
        name = _route_name(path, taken_names)
        plan.entrypoints.append(
            Entrypoint(path=path, name=name, file=f"code/{name}.py")
        )

    # rawUrl and readFile both resolve to read-only bundled assets. De-duplicate by the
    # LITERAL path (not the derived key): two literals that normalize to the same key
    # (``./logo.png`` vs ``logo.png``) must BOTH appear in the manifest so the served
    # runtime — which looks up by the exact string the page passed — never 404s. They
    # share one key/file, so the bundle stores the bytes once.
    seen_asset_paths: set[str] = set()
    for method in ("rawUrl", "readFile"):
        for path in _literal_paths(html, method):
            if path in seen_asset_paths:
                continue
            if not _reject_unsafe_rel(path, method, plan.errors):
                continue
            src = os.path.join(page_dir, path)
            if not _within_page_dir(page_dir, src):
                plan.errors.append(
                    f"{method} target {path!r} resolves outside the page directory "
                    "(a symlink escaping the bundle); only files under the page can be bundled"
                )
                continue
            if not os.path.isfile(src):
                plan.errors.append(f"{method} target {path!r} not found next to the page ({src})")
                continue
            # removeprefix, NOT lstrip: lstrip("./") strips any leading run of
            # '.'/'/' chars, mangling dotfiles ("./.env" -> "env"). normpath
            # already collapses "./x" to "x"; this only guards a residual "./".
            key = os.path.normpath(path).replace(os.sep, "/").removeprefix("./")
            seen_asset_paths.add(path)
            plan.assets.append(Asset(path=path, name=key, file=f"assets/{key}"))

    return plan


def _manifest(plan: ExportPlan, page_file: str) -> dict:
    """The bundle's ``manifest.json`` — the contract the hosting layer reads.

    ``entrypoints`` maps each ``runPython`` literal path to its served route name and
    bundled file; ``assets`` does the same for ``rawUrl``/``readFile`` targets. The
    hosting layer uses this to wire the served page's runtime (which literal path posts
    to which route) without re-parsing the HTML.
    """
    return {
        "fused_render_bundle": 1,
        "page": page_file,
        "entrypoints": [
            {"path": e.path, "name": e.name, "file": e.file} for e in plan.entrypoints
        ],
        "assets": [{"path": a.path, "name": a.name, "file": a.file} for a in plan.assets],
    }


def export_page(html_path: str, out_dir: str) -> ExportPlan:
    """Export the page at ``html_path`` into a portable bundle at ``out_dir``.

    Writes ``page.html``, ``manifest.json``, ``code/<name>.py`` per ``runPython`` target,
    and ``assets/<key>`` per ``rawUrl``/``readFile`` target. Raises :class:`ExportError`
    on any blocking problem (dynamic path, unsupported API, unsafe/missing file) with all
    problems listed at once. Returns the realized :class:`ExportPlan` on success.
    """
    import json

    html_path = os.path.abspath(html_path)
    if not os.path.isfile(html_path):
        raise ExportError(f"no such file: {html_path}")
    ext = os.path.splitext(html_path)[1].lower()
    if ext not in (".html", ".htm"):
        raise ExportError(f"{html_path} is not an .html/.htm file")

    with open(html_path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    page_dir = os.path.dirname(html_path)

    plan = plan_export(html, page_dir)
    if plan.errors:
        raise ExportError(
            "cannot export "
            + os.path.basename(html_path)
            + ":\n  - "
            + "\n  - ".join(plan.errors)
        )

    os.makedirs(out_dir, exist_ok=True)
    page_file = "page.html"

    # Stage the whole bundle first and only swap it into place once every copy
    # and the manifest write has succeeded — otherwise a mid-export failure
    # (missing file, disk full) could leave code/assets cleared or partially
    # rewritten under a stale manifest.json that no longer matches them.
    with tempfile.TemporaryDirectory(prefix=".fused-render-export-", dir=out_dir) as stage:
        shutil.copyfile(html_path, os.path.join(stage, page_file))

        if plan.entrypoints:
            os.makedirs(os.path.join(stage, "code"), exist_ok=True)
        for e in plan.entrypoints:
            shutil.copyfile(os.path.join(page_dir, e.path), os.path.join(stage, e.file))

        for a in plan.assets:
            dest = os.path.join(stage, a.file)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.join(page_dir, a.path), dest)

        with open(os.path.join(stage, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(_manifest(plan, page_file), f, indent=2, sort_keys=True)
            f.write("\n")

        # Everything staged successfully — now replace the bundle-owned paths.
        # A previous bundle's code/assets may hold files the new manifest no
        # longer lists, so those two subdirs are cleared before the move;
        # anything else the user has in --out is left untouched.
        for owned in ("code", "assets"):
            shutil.rmtree(os.path.join(out_dir, owned), ignore_errors=True)
            staged = os.path.join(stage, owned)
            if os.path.isdir(staged):
                shutil.move(staged, os.path.join(out_dir, owned))
        shutil.move(os.path.join(stage, page_file), os.path.join(out_dir, page_file))
        shutil.move(os.path.join(stage, "manifest.json"), os.path.join(out_dir, "manifest.json"))

    return plan
