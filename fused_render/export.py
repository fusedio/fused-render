"""Export a renderable HTML page into a portable bundle for hosted serving.

fused-render is local-only by design (SPEC §1 "Never deployed to cloud"): the
server binds 127.0.0.1 and hosts nothing. This module does **not** change that.
It adds a pure, local *build* step — called via ``POST /api/export`` on the
already-running server (D71; see server.py) — that statically collects a page's
dependencies into a self-contained bundle directory. A separate hosting layer
(the ``fused`` wheel's ``build_html_artifact``) turns that bundle into a served
app; nothing here uploads or phones home.

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

import ast
import fnmatch
import json
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

# A `fused.<method>(` call whose first argument is a COMPLETE quoted string literal — the
# string, then (modulo whitespace) a `,` or `)`. The named group `litd`/`lits` holds the
# contents (double- vs single-quoted). Two things make this precise:
#   * The body **excludes its own delimiter** (`[^"\\]` / `[^'\\]`, with `\\.` for escapes),
#     so it cannot span across a `" + x + "`. This is what separates a real literal target
#     from a string that is merely the PREFIX of a computed expression: `fused.rawUrl(
#     "data/" + name)`, `"data/" + name + ".json"`, and `"data/" + foo("x")` all fail to
#     match (the body stops at the first inner quote and the following `+` isn't `,`/`)`),
#     so they fall through to the dynamic (computed-path) count instead of being
#     mis-collected as a bogus `data/`-ish asset target. Separate quote branches let a
#     double-quoted path contain an apostrophe (and vice-versa).
#   * The `(?=\s*[,)])` lookahead requires the string to be the whole first argument.
# `\s*` before the `(` tolerates `fused.runPython (...)` — valid JS a page author could
# write, which must not silently vanish from export.
_LITERAL_CALL = {
    method: re.compile(
        r"fused\.%s\s*\(\s*"
        r"""(?:"(?P<litd>(?:[^"\\]|\\.)*)"|'(?P<lits>(?:[^'\\]|\\.)*)')"""
        r"(?=\s*[,)])" % method
    )
    for method in ("runPython", "rawUrl", "readFile")
}
# Any `fused.<method>(` occurrence, literal or not — used to detect dynamic paths
# (an occurrence not covered by the literal match above).
_ANY_CALL = {method: re.compile(r"fused\.%s\s*\(" % method) for method in _LITERAL_CALL}

# Unsupported API surface: present in an exported page => hard error.
_UNSUPPORTED = re.compile(r"fused\.(writeFile|stat)\s*\(")

# Bundle v2 payload directory. Every bundled file lives at ``<PAYLOAD>/<page-relative
# path>`` — one directory mirroring the author's folder, instead of the v1 ``code/``/
# ``assets/``/``resources/`` category dirs. The hosting layer strips this prefix so each
# file lands at its real relative path under the served project root (docs/bundle-v2-design.md).
_PAYLOAD_DIR = "files"

# The page-adjacent bundle manifest: a single ``<script type="application/fused-bundle">``
# block carrying a JSON object. Group 2 is the JSON body. Case-insensitive (HTML attrs)
# and DOTALL (the JSON spans lines); the ``type`` attribute is the discriminator, so the
# manifest carries NO version field — it is forward-lenient instead (unknown keys ignored,
# so new directives can be added later without breaking an older exporter). Today it reads
# only ``include`` (globs + literal page-relative paths bundled as read-only assets), which
# leaks nothing a hosted page doesn't already expose (the served asset map enumerates every
# file the globs resolve to). ``exclude`` is deliberately NOT honored here — it would name
# withheld files in the public page source — so it is warned about, not applied; drop files
# via the Deploy modal / ``/api/export`` ``exclude`` (kept on the deployment record, off the
# artifact). The block is stripped before the dependency scan so its JSON body can never be
# misread as a ``fused.*`` call.
_BUNDLE_MANIFEST = re.compile(
    r"<script\b[^>]*\btype\s*=\s*(['\"])application/fused-bundle\1[^>]*>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
# A path entry containing `*` or `?` is treated as a glob (expanded against the page dir);
# otherwise it is a literal page-relative path (validated like an explicit include). `[`/`]`
# are deliberately NOT triggers: a literal filename with brackets is common (e.g. a browser
# "file[1].json" duplicate download), and treating it as a character class would silently
# omit the real file (zero-match warning) instead of bundling it / erroring on a true miss.
# A bracket used as an intentional character class still works inside a pattern that already
# has a `*`/`?`.
_GLOB_META = re.compile(r"[*?]")


class ExportError(Exception):
    """A user-correctable failure while exporting a page (POST /api/export
    returns its message verbatim as a 400 {"error"})."""


@dataclass(frozen=True)
class Entrypoint:
    """A ``runPython`` target: the literal path in the page, its bundled file, and the
    served route name the hosting layer will expose (what the page POSTs to)."""

    path: str  # the literal string passed to runPython, e.g. "./sine.py"
    name: str  # the served route name, e.g. "sine"
    file: str  # bundle-relative destination, e.g. "files/sine.py"


@dataclass(frozen=True)
class Asset:
    """A read-only file bundled and served by the ``_asset`` route — the surface
    ``fused.rawUrl``/``readFile`` fetch from.

    ``source`` records WHY the file is in the bundle, so the Deploy modal's "Will
    publish" list can say whether the page is *known* to fetch it via
    ``rawUrl``/``readFile`` versus bundled to back a computed path or added by hand:

      * ``"reference"`` — a literal ``fused.rawUrl()``/``readFile()`` argument the
        HTML scan resolved: the page fetches this file via rawUrl/readFile.
      * ``"manifest"`` — declared in the page's embedded
        ``<script type="application/fused-bundle">`` include (globs/literals), the
        reproducible way to back a *computed* rawUrl/readFile path (EX-4a/EX-8).
      * ``"include"`` — added out-of-band via the caller's / Deploy modal's
        ``include`` (e.g. "Add all in folder"), not seen in the HTML at all.

    A file reachable more than one way is attributed to the first that claims it,
    in that order (reference > manifest > include), and bundled once."""

    path: str  # the literal string passed to rawUrl/readFile, e.g. "./logo.png"
    name: str  # the asset key the page requests, e.g. "logo.png"
    file: str  # bundle-relative destination, e.g. "files/logo.png"
    source: str = "reference"  # "reference" | "manifest" | "include" (see above)


@dataclass(frozen=True)
class Resource:
    """A first-party Python module a bundled entrypoint ``import``s (directly or
    transitively).

    Shipped into the served page's runtime tree at its real page-relative path so a bare
    ``import helpers`` resolves — but, unlike an :class:`Asset`, a page never *fetches* it,
    so the hosting layer ships it as a plain resource file and does **not** put it on the
    ``_asset`` allow-list (its source is not web-readable)."""

    key: str  # runtime-relative path == import location, e.g. "helpers.py"
    file: str  # bundle-relative destination, e.g. "files/helpers.py"


@dataclass
class ExportPlan:
    """What an export will bundle, plus any problems found while scanning.

    ``errors`` are blocking (``export_page`` raises; Deploy is disabled). ``warnings``
    are advisory and never block — a computed ``rawUrl``/``readFile`` path (whose target
    the user can bundle via an explicit include) or an ``exclude`` that drops a
    literally-referenced file (which will 404 when hosted). The user chose to ship it;
    the note explains the consequence.
    """

    entrypoints: list[Entrypoint] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _slugify(stem: str) -> str:
    """Lowercase, mapping runs of non-``[a-z0-9]`` to single hyphens: ``My_File`` → ``my-file``."""
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


_RESERVED_SLUGS = frozenset(_slugify(n) for n in _RESERVED_NAMES)


def _route_name(rel_path: str, taken: set[str]) -> str:
    """A unique, valid, non-reserved route name derived from a ``.py`` path.

    Slugifies the filename stem; falls back to ``run`` when nothing survives, prefixes
    ``run-`` when the slug is reserved or host-internal (leading ``_``), and appends
    ``-2``, ``-3``, … on collision so two files with the same stem stay distinct.

    Reserved names are matched by their *slugified* form (``_RESERVED_SLUGS``), not the
    literal ``_RESERVED_NAMES`` strings — ``_slugify`` maps a leading ``_`` to a hyphen
    and then strips it, so e.g. ``_shell.py`` slugifies to ``"shell"``, which would never
    match the literal ``"_shell"``.
    """
    base = _slugify(os.path.splitext(os.path.basename(rel_path))[0]) or "run"
    if base in _RESERVED_SLUGS or base.startswith("-"):
        base = f"run-{base.lstrip('-')}"
    name = base
    n = 2
    while name in taken:
        name = f"{base}-{n}"
        n += 1
    taken.add(name)
    return name


def _asset_key(path: str) -> str:
    """The bundle-relative asset key for a page-relative ``path``.

    ``removeprefix``, NOT ``lstrip``: ``lstrip("./")`` strips any leading run of
    ``.``/``/`` chars, mangling dotfiles (``./.env`` -> ``env``). ``normpath`` already
    collapses ``./x`` to ``x``; this only guards a residual ``./``. Shared by the
    ``rawUrl``/``readFile`` scan and the manual-include loop so a file reachable both
    ways lands on the same key (and is bundled once)."""
    return os.path.normpath(path).replace(os.sep, "/").removeprefix("./")


def _literal_paths(html: str, method: str) -> list[str]:
    """Ordered, de-duplicated literal path arguments to ``fused.<method>(`` in ``html``."""
    seen: dict[str, None] = {}
    for m in _LITERAL_CALL[method].finditer(html):
        # Exactly one of the quote branches matched (double `litd` / single `lits`).
        content = m.group("litd")
        if content is None:
            content = m.group("lits")
        seen.setdefault(content, None)
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


def _extract_bundle_manifest(html: str, errors: list[str], warnings: list[str]) -> tuple[list[str], str]:
    """Pull the embedded ``<script type="application/fused-bundle">`` manifest out of ``html``.

    Returns ``(include_entries, html_without_the_block)``. The block is stripped from the
    returned HTML so its JSON body can never be misread by the ``fused.*`` dependency scan
    (a value shaped like ``fused.writeFile(…)`` would otherwise trip a spurious error).

    The manifest is intentionally **unversioned and forward-lenient**: the ``type``
    attribute identifies it, and unknown keys are ignored so new directives can be added
    later without breaking an older exporter. Only ``include`` is read today (globs +
    literal page-relative paths). ``exclude`` is a **warning**, not applied — honoring it
    here would publish the names of withheld files in the served page source; drop files via
    the Deploy modal / ``/api/export`` ``exclude`` instead. A malformed block (multiple
    blocks, non-JSON, wrong shape) is a blocking error.
    """
    matches = list(_BUNDLE_MANIFEST.finditer(html))
    if not matches:
        return [], html
    stripped = _BUNDLE_MANIFEST.sub("", html)
    if len(matches) > 1:
        errors.append(
            'multiple <script type="application/fused-bundle"> blocks found; a page may '
            "declare at most one bundle manifest"
        )
        return [], stripped
    try:
        data = json.loads(matches[0].group(2))
    except ValueError as exc:
        errors.append(f'the <script type="application/fused-bundle"> manifest is not valid JSON: {exc}')
        return [], stripped
    if not isinstance(data, dict):
        errors.append('the fused-bundle manifest must be a JSON object')
        return [], stripped
    include = data.get("include", [])
    if not isinstance(include, list) or not all(isinstance(x, str) for x in include):
        errors.append("the fused-bundle manifest 'include' must be an array of strings")
        return [], stripped
    if "exclude" in data:
        warnings.append(
            "the fused-bundle manifest 'exclude' is ignored — excluding here would publish "
            "the withheld file names in the served page; drop files via the Deploy modal or "
            "the /api/export 'exclude' field instead"
        )
    return include, stripped


def _glob_segments_match(path_segs: list[str], pat_segs: list[str]) -> bool:
    """Glob-match a file's path segments against pattern segments.

    ``*``/``?``/``[…]`` match within a single segment (via :func:`fnmatch.fnmatch`, so they
    never cross ``/``); ``**`` spans zero or more whole segments (``data/**/*.json`` matches
    ``data/x.json`` and ``data/a/b/x.json``). This is the segment-wise glob semantics used in
    place of :func:`glob.glob` so expansion can walk with symlinks disabled (below)."""
    if not pat_segs:
        return not path_segs
    head, rest = pat_segs[0], pat_segs[1:]
    if head == "**":
        if not rest:
            return True  # trailing ** matches everything below here
        return any(_glob_segments_match(path_segs[i:], rest) for i in range(len(path_segs) + 1))
    if path_segs and fnmatch.fnmatch(path_segs[0], head):
        return _glob_segments_match(path_segs[1:], rest)
    return False


def _glob_in_page(page_dir: str, pattern: str) -> list[str]:
    """Page-relative files matching ``pattern``, walking ``page_dir`` **without following
    directory symlinks** (``os.walk(followlinks=False)``).

    Not following directory symlinks keeps the walk inside the real page tree: a
    ``data/**`` glob can't be lured by a ``data/link -> /huge/tree`` symlink into scanning
    (or hanging on) an external tree, and it won't surface an out-of-tree file that
    ``_within_page_dir`` would then have to turn into a blocking error. A symlinked *file*
    is still yielded (cheap) and rejected downstream by the caller's gauntlet, as before."""
    pat_segs = [s for s in pattern.replace(os.sep, "/").split("/") if s != ""]
    hits: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(page_dir, followlinks=False):
        rel_dir = os.path.relpath(dirpath, page_dir).replace(os.sep, "/")
        for fn in filenames:
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            if _glob_segments_match(rel.split("/"), pat_segs):
                hits.append(rel)
    return hits


def _expand_manifest_include(
    page_dir: str, entries: list[str], errors: list[str], warnings: list[str]
) -> list[str]:
    """Resolve manifest ``include`` entries (globs and literal paths) to page-relative files.

    A glob (contains ``*`` or ``?``) is expanded against ``page_dir`` (via :func:`_glob_in_page`,
    which walks with directory symlinks disabled) to page-relative, forward-slash paths; a
    glob matching nothing is a **warning** (a declaration of intent may legitimately match
    nothing yet), never an error. A literal entry is passed through unchanged — missing or
    unsafe literals surface downstream as blocking errors, exactly like an explicit
    ``/api/export`` include.

    An **absolute or ``..``-escaping glob pattern is rejected up front** (same
    :func:`_reject_unsafe_rel` error a literal gets) and **not expanded**, so the walk never
    starts outside the page tree. Order is preserved and duplicates dropped; every surviving
    result still runs the caller's full safety gauntlet (``_reject_unsafe_rel`` /
    ``_within_page_dir``), so a relative glob still can't smuggle in a (file) symlink escape.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(rel: str) -> None:
        if rel not in seen:
            seen.add(rel)
            out.append(rel)

    for entry in entries:
        if _GLOB_META.search(entry):
            # Validate the PATTERN before expanding, so an absolute/`..` glob never walks
            # outside the page tree and reports the same failure a literal would.
            if not _reject_unsafe_rel(entry, "manifest include glob", errors):
                continue
            hits = sorted(_glob_in_page(page_dir, entry))
            if not hits:
                warnings.append(
                    f"the fused-bundle manifest glob {entry!r} matched no files under the page"
                )
            for rel in hits:
                _add(rel)
        else:
            _add(entry)
    return out


def _local_module_imports(source: str) -> list[str]:
    """Ordered, de-duplicated top-level module names ``source`` imports **absolutely**.

    ``import a`` / ``import a.b as c`` / ``from a import x`` / ``from a.b import y`` all
    contribute ``a`` (the top-level name — only ``a.py`` beside the page can be bundled;
    ``a.b`` as a subpackage is out of scope). Relative imports (``from . import x``,
    ``level > 0``) are skipped: a hosted entrypoint runs flattened at the project root
    with no package context, so only absolute imports of a sibling module resolve. Parsing
    is static (``ast``) and side-effect-free — a file that does not parse yields nothing
    (its own ``SyntaxError`` surfaces when the page runs it, not here)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    seen: dict[str, None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.setdefault(alias.name.split(".", 1)[0], None)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            seen.setdefault(node.module.split(".", 1)[0], None)
    return list(seen)


def _discover_modules(
    page_dir: str,
    entrypoints: list[Entrypoint],
    asset_keys: set[str],
    exclude: list[str],
) -> tuple[list[Resource], list[str]]:
    """Find first-party sibling modules the bundled ``entrypoints`` import, transitively.

    BFS over the ``import`` statements of each entrypoint source (and of each module it
    pulls in), resolving every absolute top-level name to a ``<name>.py`` **beside the
    page** — hosted entrypoints run flattened at the project root, so imports resolve from
    the root regardless of the importer's own subdirectory. A name that resolves to no
    local file (stdlib / third-party / a subpackage) is silently left alone. A module
    already shipped as an asset (``asset_keys`` — assets land at the same real path, so the
    import already resolves) is skipped so it is bundled once. Returns ``(resources,
    warnings)``; excluding a module a bundled entrypoint imports is honored but warned (the
    import will fail when hosted)."""
    drop = {_asset_key(p) for p in exclude} | set(exclude)
    resources: list[Resource] = []
    warnings: list[str] = []
    seen_keys = set(asset_keys)
    scanned: set[str] = set()
    queue = [os.path.join(page_dir, e.path) for e in entrypoints]
    while queue:
        src_path = queue.pop(0)
        real = os.path.realpath(src_path)
        if real in scanned:
            continue
        scanned.add(real)
        try:
            with open(src_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError:
            continue
        for mod in _local_module_imports(source):
            key = mod + ".py"
            if key in seen_keys:
                continue
            cand = os.path.join(page_dir, key)
            if not (os.path.isfile(cand) and _within_page_dir(page_dir, cand)):
                continue  # not a first-party sibling module — nothing to bundle
            seen_keys.add(key)
            if key in drop:
                warnings.append(
                    f"excluding module {key!r}, which a bundled entrypoint imports — that "
                    "import will fail on the hosted page"
                )
                continue
            resources.append(Resource(key=key, file=f"{_PAYLOAD_DIR}/{key}"))
            queue.append(cand)
    return resources, warnings


def plan_export(
    html: str,
    page_dir: str,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> ExportPlan:
    """Scan a page's HTML and build an :class:`ExportPlan` (pure — no files written).

    Resolves each literal ``runPython``/``rawUrl``/``readFile`` path against ``page_dir``,
    recording blocking problems (dynamic ``runPython`` paths, unsupported API,
    unsafe/missing files) in ``plan.errors`` rather than raising — so a caller can report
    them all at once.

    The auto-detected set is then adjusted by the user's selection:

      * ``include`` — extra page-relative files to bundle as read-only assets (beyond
        the literal ``rawUrl``/``readFile`` scan), from the caller AND from the page's
        embedded manifest (globs expanded, folded in first). Each goes through the same
        safety gauntlet as a scanned asset; a bad one is a blocking error. This is how a
        file reached by a *computed* path (a warning, below) or read at runtime by a
        bundled ``.py`` actually gets into the bundle.
      * ``exclude`` — page-relative paths (or their bundle key) to drop from the final
        set. Dropping a literally-referenced target is honored but warned (that call
        404s when hosted).

    A computed (non-literal) ``rawUrl``/``readFile`` path is a **warning**, not an error:
    the exporter can't discover the target, but the user can bundle it via ``include`` — or,
    reproducibly, via a page-adjacent ``<script type="application/fused-bundle">`` manifest
    whose ``include`` globs are expanded here and folded in **beneath** the caller's
    ``include`` (see :func:`_extract_bundle_manifest`). Once the target is bundled, the
    hosted ``_asset`` route resolves it by key, so a runtime-computed ``rawUrl`` path works.
    A computed ``runPython`` path stays a hard error (its served route name is derived
    from the literal path — there is nothing to route a computed call to).

    Finally, first-party **modules** the bundled entrypoints ``import`` (transitively) are
    discovered by a static AST scan (:func:`_discover_modules`) and recorded in
    ``plan.resources`` — sibling ``.py`` files shipped so a hosted entrypoint's
    ``import helpers`` resolves, without the author hand-listing them. Unlike assets they
    are runtime-only (never web-served). Only absolute imports resolving to a ``<name>.py``
    beside the page are bundled; stdlib/third-party imports and subpackages are left alone.
    """
    plan = ExportPlan()
    include = include or []
    exclude = exclude or []

    # The embedded manifest is read first: its block is stripped from `html` (so its JSON
    # body can't false-positive in the scans below) and its expanded `include` globs are
    # prepended to the caller's include list (both are just added assets; exclude runs last).
    # The prepend order also fixes provenance: a key declared in BOTH the manifest and the
    # caller's include is deduped to the manifest entry (first wins), so it is attributed to
    # the page's own reproducible declaration rather than the ad-hoc selection.
    manifest_include, html = _extract_bundle_manifest(html, plan.errors, plan.warnings)
    manifest_include = _expand_manifest_include(page_dir, manifest_include, plan.errors, plan.warnings)
    manifest_keys = {_asset_key(p) for p in manifest_include}
    include = manifest_include + include

    for m in _UNSUPPORTED.finditer(html):
        api = m.group(1)
        plan.errors.append(
            f"fused.{api}() is not supported on a hosted page (a served artifact is "
            "immutable and has no filesystem); remove it before exporting"
        )

    dyn_run = _dynamic_call_count(html, "runPython")
    if dyn_run > 0:
        plan.errors.append(
            f"{dyn_run} fused.runPython() call(s) use a non-literal (computed) path; a "
            "hosted entrypoint's route name is derived from its literal path, so a "
            "computed runPython target cannot be bundled or routed"
        )
    # The computed-path advisory is emitted AFTER the asset passes below (it depends on the
    # FINAL bundle), near the end of this function.

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
            Entrypoint(path=path, name=name, file=f"{_PAYLOAD_DIR}/{_asset_key(path)}")
        )

    # rawUrl and readFile both resolve to read-only bundled assets. De-duplicate by the
    # LITERAL path (not the derived key): two literals that normalize to the same key
    # (``./logo.png`` vs ``logo.png``) must BOTH appear in the manifest so the served
    # runtime — which looks up by the exact string the page passed — never 404s. They
    # share one key/file, so the bundle stores the bytes once.
    seen_asset_paths: set[str] = set()
    seen_asset_keys: set[str] = set()
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
            key = _asset_key(path)
            seen_asset_paths.add(path)
            seen_asset_keys.add(key)
            plan.assets.append(
                Asset(path=path, name=key, file=f"{_PAYLOAD_DIR}/{key}", source="reference")
            )

    # Manual includes: extra files bundled as assets, keyed the same way. A file already
    # brought in by the literal scan (same key) is skipped — bundled once. A file already
    # bundled as a runPython ENTRYPOINT is skipped too (compare by asset key): under v2 the
    # entrypoint already lives at files/<key>, so the concern is not duplicate bytes but the
    # asset role — adding it as an asset would put that key on the read-only ``_asset``
    # allow-list, web-exposing the entrypoint's source to GET when it should only be
    # reachable as a POST-executed route. An unsafe or missing include is a blocking error,
    # like a scanned asset that doesn't exist.
    entrypoint_keys = {_asset_key(e.path) for e in plan.entrypoints}
    for path in include:
        key = _asset_key(path)
        if key in seen_asset_keys or key in entrypoint_keys:
            continue
        if not _reject_unsafe_rel(path, "included file", plan.errors):
            continue
        src = os.path.join(page_dir, path)
        if not _within_page_dir(page_dir, src):
            plan.errors.append(
                f"included file {path!r} resolves outside the page directory "
                "(a symlink escaping the bundle); only files under the page can be bundled"
            )
            continue
        if not os.path.isfile(src):
            plan.errors.append(f"included file {path!r} not found next to the page ({src})")
            continue
        seen_asset_keys.add(key)
        # A manifest-declared file is the page's own reproducible bundle declaration
        # (it backs a computed rawUrl/readFile path); a caller/modal include is ad-hoc.
        source = "manifest" if key in manifest_keys else "include"
        plan.assets.append(
            Asset(path=path, name=key, file=f"{_PAYLOAD_DIR}/{key}", source=source)
        )

    # Excludes drop matching entrypoints/assets by their literal path OR bundle key.
    # Dropping something the page literally references is the user's call, but warned —
    # the served page's call to it will 404. Manually-included files (not referenced)
    # drop silently.
    if exclude:
        drop_keys = {_asset_key(p) for p in exclude}
        drop_raw = set(exclude)

        def _excluded(path: str) -> bool:
            return path in drop_raw or _asset_key(path) in drop_keys

        kept_entrypoints = []
        for e in plan.entrypoints:
            if _excluded(e.path):
                plan.warnings.append(
                    f"excluding {e.path!r}, which the page runs via fused.runPython() — "
                    "that call will fail on the hosted page"
                )
            else:
                kept_entrypoints.append(e)
        plan.entrypoints = kept_entrypoints

        kept_assets = []
        for a in plan.assets:
            if _excluded(a.path):
                if a.path in seen_asset_paths:  # a literally-referenced asset, not just an include
                    plan.warnings.append(
                        f"excluding {a.path!r}, which the page fetches via "
                        "fused.rawUrl()/readFile() — that fetch will 404 on the hosted page"
                    )
            else:
                kept_assets.append(a)
        plan.assets = kept_assets

    # A computed rawUrl/readFile path can't be resolved from the HTML — advisory, never
    # blocking. Emitted HERE, after dedup and exclude, so it reflects the FINAL bundle:
    # suppressed only when a `manifest`-source asset actually SURVIVED — a "bundle" badge in
    # the Deploy list (§19) that shows the user what backs the call. Keyed on the surviving
    # assets, NOT the raw manifest globs, because a manifest entry can fail to leave a
    # `manifest` row: one that is also a literal reference is deduped to a `reference` asset,
    # and any manifest file can be dropped by `exclude`. In both cases no "bundle" row
    # remains, so the justification ("the list shows what backs it") does not hold and the
    # nag must still fire. A per-deployment `include` (source `include`, e.g. "Add all in
    # folder") never suppresses it either: that ad-hoc selection is not checked in with the
    # page, so a fresh export without it would still 404 — only the manifest travels along.
    dyn_asset = sum(_dynamic_call_count(html, method) for method in ("rawUrl", "readFile"))
    if dyn_asset > 0 and not any(a.source == "manifest" for a in plan.assets):
        plan.warnings.append(
            f"{dyn_asset} fused.rawUrl()/readFile() call(s) use a computed path the "
            "exporter can't resolve — declare the files those calls fetch in a "
            '<script type="application/fused-bundle"> manifest ("include" globs), or add '
            'them under "Include files" ("Add all in folder"), so they are bundled and '
            "served (the hosted _asset route then resolves the computed path by key)"
        )

    # Ship first-party modules the (surviving) entrypoints import, so `import helpers`
    # resolves on the hosted page with no hand-listing. Discovered after excludes so a
    # dropped entrypoint is not scanned, and against the final asset key set so a module
    # already carried as an asset is not bundled twice.
    plan.resources, module_warnings = _discover_modules(
        page_dir, plan.entrypoints, {a.name for a in plan.assets}, exclude
    )
    plan.warnings += module_warnings

    return plan


def _manifest(plan: ExportPlan, page_key: str, cache_max_age: str) -> dict:
    """The bundle's ``manifest.json`` (v2) — the contract the hosting layer reads.

    v2 lays every bundled file under a single ``root`` payload dir at its real page-relative
    path (== its runtime key), so the bundle mirrors the author's folder and the physical
    ``file`` location is just ``root/<key>`` (no separate ``file`` field, no ``code/``/
    ``assets/``/``resources/`` category dirs — docs/bundle-v2-design.md).

    - ``page`` — the shell HTML's payload-relative path.
    - ``entrypoints`` — each ``runPython`` target: ``path`` is the page's literal string (the
      hosting layer's seed maps literal→route, so it must be preserved verbatim), ``name`` is
      the served route, ``key`` is the payload-relative path the source is read from.
    - ``assets`` — each ``rawUrl``/``readFile`` target: ``path`` is the literal, ``name`` is
      the payload-relative key (also the ``_asset`` allow-list entry). Two distinct literals
      may share a key — both appear so the runtime's exact-string lookup never misses.
    - ``resources`` — imported modules: ``key`` is the payload-relative path (never web-served).
    - ``cache_max_age`` — the deploy-time result-caching choice (``"0s"`` off by default),
      e.g. ``"5m"``/``"1h"``. Applied uniformly to every ``runPython`` route by the hosting
      layer's ``build_html_artifact`` (never the shell or asset routes); see
      spec/serve/fused-render.md § Caching in the fused repo.
    """
    return {
        "fused_render_bundle": 2,
        "root": _PAYLOAD_DIR,
        "page": page_key,
        "entrypoints": [
            {"path": e.path, "name": e.name, "key": _asset_key(e.path)}
            for e in plan.entrypoints
        ],
        "assets": [{"path": a.path, "name": a.name} for a in plan.assets],
        "resources": [{"key": r.key} for r in plan.resources],
        "cache_max_age": cache_max_age,
    }


_CACHE_MAX_AGE_RE = re.compile(r"^(\d+)([smhd])$")


def _validate_cache_max_age(value: str) -> None:
    """Reject a malformed ``cache_max_age`` before it lands in a manifest.

    Mirrors the hosting layer's own parser (the ``fused`` wheel's
    ``openfused.caching.parse_cache_max_age`` — not imported here, since ``fused`` is an
    optional extra export must work without): a non-negative integer + unit (``s``/``m``/
    ``h``/``d``), ``"0s"`` the off sentinel. Validating at export time (not deferred to
    `share create`) means a bad value fails the local, no-network step, not a later publish.
    """
    if not isinstance(value, str) or not _CACHE_MAX_AGE_RE.match(value):
        raise ExportError(
            f"invalid cache_max_age {value!r}: expected a non-negative integer with a unit "
            "(s/m/h/d), e.g. '0s', '90s', '15m', '24h', '7d'."
        )


def export_page(
    html_path: str,
    out_dir: str,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    cache_max_age: str = "0s",
) -> ExportPlan:
    """Export the page at ``html_path`` into a portable bundle at ``out_dir`` (bundle v2).

    Writes ``manifest.json`` and a single ``files/`` payload dir mirroring the page's folder:
    the page, each ``runPython`` target, each ``rawUrl``/``readFile`` target (plus any
    ``include`` files, minus any ``exclude`` — see :func:`plan_export`), and each first-party
    module a bundled entrypoint imports, all at their real page-relative path. ``cache_max_age``
    (``"0s"`` off by default, e.g. ``"5m"``) is the deploy-time result-caching choice, written
    into the manifest and applied by the hosting layer **page-wide** — to every served route
    uniformly (the shell, each ``runPython`` route, and the asset route) — see
    spec/serve/fused-render.md § Caching in the fused repo.
    Raises :class:`ExportError` on any blocking problem (dynamic runPython path, unsupported
    API, unsafe/missing file, malformed ``cache_max_age``) with all problems listed at once;
    advisory ``plan.warnings`` never block. Returns the realized :class:`ExportPlan` on success.
    """
    _validate_cache_max_age(cache_max_age)
    html_path = os.path.abspath(html_path)
    if not os.path.isfile(html_path):
        raise ExportError(f"no such file: {html_path}")
    ext = os.path.splitext(html_path)[1].lower()
    if ext not in (".html", ".htm"):
        raise ExportError(f"{html_path} is not an .html/.htm file")

    with open(html_path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    page_dir = os.path.dirname(html_path)

    plan = plan_export(html, page_dir, include=include, exclude=exclude)
    if plan.errors:
        raise ExportError(
            "cannot export "
            + os.path.basename(html_path)
            + ":\n  - "
            + "\n  - ".join(plan.errors)
        )

    # Export is **non-destructive**: it writes a self-contained bundle and NEVER deletes an
    # existing file, so the out dir must be empty (or not yet exist). A non-empty out dir is
    # refused outright — this makes it impossible to clobber an author's files (the page's own
    # folder, an ancestor of it, or any dir with content) with no fragile "is this a prior
    # bundle?" heuristic to get wrong. Deploy always targets a fresh temp dir; to re-export,
    # point at a new/empty directory.
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise ExportError(
            f"cannot export into {out_dir}: the output directory must be empty (export writes "
            "a self-contained bundle and never deletes existing files). Choose a new or empty "
            "directory."
        )

    page_key = os.path.basename(html_path)  # payload-relative path of the page

    def _copy_into(stage: str, src: str, rel: str) -> None:
        dest = os.path.join(stage, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)

    # Build the whole bundle in a temp dir alongside out_dir (same filesystem), then swap it
    # in with ONE atomic rename. So out_dir is only ever absent/empty or the COMPLETE bundle:
    # a failure mid-build leaves a partial tree only in the temp dir (cleaned up in `finally`),
    # never in out_dir, so a retry to the same path always works. Staging in out_dir's PARENT
    # (not inside it) keeps the emptiness check honest and leaves no hidden dir inside out_dir.
    parent = os.path.dirname(os.path.abspath(out_dir))
    os.makedirs(parent, exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".fused-render-export-", dir=parent)
    try:
        # The page + every bundled file lands under the single payload dir at its real
        # page-relative path (e.file / a.file / r.file are already f"{_PAYLOAD_DIR}/…").
        _copy_into(stage, html_path, f"{_PAYLOAD_DIR}/{page_key}")
        for e in plan.entrypoints:
            _copy_into(stage, os.path.join(page_dir, e.path), e.file)
        for a in plan.assets:
            _copy_into(stage, os.path.join(page_dir, a.path), a.file)
        for r in plan.resources:
            _copy_into(stage, os.path.join(page_dir, r.key), r.file)

        with open(os.path.join(stage, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(_manifest(plan, page_key, cache_max_age), f, indent=2, sort_keys=True)
            f.write("\n")

        # Atomic handoff: drop the (empty) out_dir if it exists so the rename can take the
        # name, then rename the fully-built stage dir into place in one step.
        if os.path.isdir(out_dir):
            os.rmdir(out_dir)  # empty (checked above)
        os.replace(stage, out_dir)
    finally:
        shutil.rmtree(stage, ignore_errors=True)  # no-op after a successful os.replace

    return plan
