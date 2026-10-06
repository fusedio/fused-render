import codecs
import json
import os
import stat as stat_mod
import sys
from fused_render.core_templates import ensure_core_templates
from fused_render.shell import storage
from fused_render.shell.storage import home_dir

from fused_render.server.common import _error, logger


# Core templates ship in the package but are staged into
# ~/.fused-render/.core-templates on startup (reset-on-release); the server
# reads every built-in template/registry/helper from that copy, not the bundle.
TEMPLATES_DIR = ensure_core_templates()

# Built-in extension → mode-list bindings ship as data, not code (D73):
# templates/registry.json, exactly the user-registry format (SPEC §16). Keys
# are dot-anchored suffix patterns — ".csv", compound ".xyz.json", wildcard
# ".*.json" (`*` = one whole dot-segment) — and a trailing "/" marks a
# directory key (".zarr/": a zarr store is one logical dataset spread across
# many chunk files, so it previews as a dataset rather than a listing).
# Values are ordered lists of template names, first = default (SPEC PT-7,
# D60). A name is a folder name (fused_render/templates/<name>/), never a
# filename. Rationale per mapping lives in the SPEC PT-7 table.
BUILTIN_REGISTRY = os.path.join(TEMPLATES_DIR, "registry.json")

# Shell sentinel modes (SPEC PT-12): implemented by the shell, no template
# folder behind them. The only `_`-prefixed names a registry mode list may
# reference (D73); any other `_` name is invalid (CT-6). `_listing` is the
# shell's built-in directory listing — the default of the universal `/`
# directory key (D81).
KNOWN_SENTINELS = {"_render", "_listing"}


# /api/fs/conditions evaluates template condition.py gates, which can do real
# I/O and was recomputed on every call. A small check-on-read TTL
# cache lets re-navigation to the same directory reuse the verdict. Only success
# payloads (plain dicts) are cached; error/404 responses are JSONResponse and are
# never stored. No background eviction — a stale entry is overwritten on the next
# miss. _CONDITIONS_TTL_S is a module attribute so tests can monkeypatch it.
_CONDITIONS_TTL_S = 60.0
# path -> (inserted_monotonic, prefs_mtime, payload). Gates may read the
# preference store (the reader template's condition.py does), so a cached
# verdict is only valid while prefs.json is unchanged — otherwise flipping a
# Preferences toggle looks dead for a full TTL.
_CONDITIONS_CACHE: dict[str, tuple[float, float, dict]] = {}


def _prefs_mtime() -> float:
    # Local import keeps module import order unchanged; shell never imports
    # server so this direction is safe.
    from fused_render.shell import storage
    try:
        return os.path.getmtime(os.path.join(storage.home_dir(), "prefs.json"))
    except OSError:
        return 0.0


# User templates + their registry live under the shell home dir's templates/
# subdir (D76) — ~/.fused-render/templates/<name>/ and .../templates/registry.json
# — one level below the home dir that also holds bookmarks.json (shell/storage).
# home_dir() itself nests per branch ref (shell/storage), so branch isolation
# comes for free here — no branch logic needed in server.
USER_TEMPLATES_DIR = os.path.join(home_dir(), "templates")
USER_REGISTRY = os.path.join(USER_TEMPLATES_DIR, "registry.json")


def _resolve_name(name):
    """Single template-name resolution rule, used identically for built-in
    table entries and registry entries (SPEC PT-6): `<name>` resolves to
    `~/.fused-render/templates/<name>/template.html` if present, else the staged
    core template `<TEMPLATES_DIR>/<name>/template.html` (core_templates), else
    unusable. A user
    folder shadows a built-in of the same name — the deliberate override
    channel. Returns (abs template.html path | None, error | None).

    PT-6 amendment (native templates): a folder with no `template.html` but a
    `native` marker file also resolves, to `<folder>/native`. Such a template's
    UI is implemented by the React shell, not an iframe page (`claude`: the
    chat is frontend/src/apps/claude, its backend fused_render/claude_agent);
    the folder exists only for registry identity, the condition.py gate
    (CT-12) and icon.svg (PT-11). The resolved path stays a FILE inside the
    folder on purpose: `_icon_for` / `_condition_file` take dirname() of it,
    so both keep working unchanged, and the shell gets a truthy `path`. Order
    is user template.html, user native, core template.html, core native — so
    within one folder the html wins, and a user folder (either form) still
    shadows a core PAGE template. A core NATIVE template is the exception:
    nothing shadows it, and a stale user template.html of the same name is
    ignored with a warning (D1310). `/render` refuses a native template (404
    "served by the shell") — and anything inside such a stale user fork, so
    the ignored page cannot be served by hand either; see
    `is_native_template_path`.
    """
    # The name is joined into a filesystem path, so it must be one plain
    # segment — a stray "../x" must not stat arbitrary locations. Correctness
    # guard, not auth (D3 stands). `.` is banned outright (SPEC CT-6): it
    # keeps names unambiguous against the "..." splice sigil and dotted
    # registry keys.
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or "\\" in name
        or "." in name
    ):
        return None, f"invalid template name: {name!r}"
    if name.startswith("_"):
        return None, (
            f"invalid template name: {name!r} — the '_' prefix is reserved "
            "for shell sentinel modes (SPEC PT-12); the only referenceable "
            "sentinel is '_render'"
        )
    # A CORE NATIVE TEMPLATE CANNOT BE SHADOWED (D1310). The one there is
    # (`claude`) used to be an iframe page, so a user who once forked it has a
    # stale `~/.fused-render/templates/claude/template.html` — and letting that
    # win would resurrect the retired page under a hand-typed /render URL and
    # hand its folder's condition.py and icon to the condition gate. The shell
    # renders the mode; a user copy has nothing left to override.
    core_marker = os.path.join(TEMPLATES_DIR, name, NATIVE_MARKER)
    if _native_folder(os.path.join(TEMPLATES_DIR, name)):
        stale = os.path.join(USER_TEMPLATES_DIR, name, "template.html")
        if stale not in _WARNED_STALE and os.path.isfile(stale):
            _WARNED_STALE.add(stale)
            logger.warning(
                "ignoring %s: %r is rendered by the shell now (D1310); "
                "delete that file to silence this", stale, name)
        return core_marker, None
    for base in (USER_TEMPLATES_DIR, TEMPLATES_DIR):
        folder = os.path.join(base, name)
        html = os.path.join(folder, "template.html")
        if os.path.isfile(html):
            return html, None
        marker = os.path.join(folder, NATIVE_MARKER)
        if os.path.isfile(marker):
            return marker, None
    return None, f"no template.html for {name!r} (looked in ~/.fused-render/templates/{name}/ and core {TEMPLATES_DIR}/{name}/)"


# Marker file naming a shell-implemented ("native") template folder (PT-6
# amendment): present instead of template.html.
NATIVE_MARKER = "native"

# Stale user template.html paths already warned about by `_resolve_name` —
# once per process, not once per /api/templates call.
_WARNED_STALE: set[str] = set()


def _is_native(template_path) -> bool:
    """True when a resolved template path is a native marker (PT-6)."""
    return bool(template_path) and os.path.basename(template_path) == NATIVE_MARKER


def _native_folder(folder: str) -> bool:
    """`folder` is a native template folder: a `native` marker, no template.html."""
    return (os.path.isfile(os.path.join(folder, NATIVE_MARKER))
            and not os.path.isfile(os.path.join(folder, "template.html")))


def is_native_template_path(path) -> bool:
    """True when `path` is a native template folder, or its `native` marker,
    directly under a template root (user, staged core, or the packaged tree),
    or ANYTHING inside a user folder named for a core-native template (a stale
    fork: its template.html is ignored by `_resolve_name`, so it must not be
    servable either). /render uses it to refuse such paths: there is no page
    to serve. The
    realpath costs an lstat per component, no more than /render's own read
    of the same path is about to."""
    if not isinstance(path, str) or not path:
        return False
    # realpath + case-folding on BOTH sides (D1310): a symlinked spelling of
    # the folder, or a differently-cased one on a case-insensitive volume (the
    # macOS default), is the same folder and must be refused the same way.
    # `normcase` folds on Windows only — on macOS it is the identity — so
    # darwin lowers explicitly. On a case-sensitive APFS volume that can only
    # widen the root match; `_native_folder` stats the real, unfolded path.
    def fold(x):
        x = os.path.normcase(x)
        return x.lower() if sys.platform == "darwin" else x

    p = os.path.realpath(path).rstrip(os.sep)
    # A USER fork of a core-native name, at any depth (D1310): `_resolve_name`
    # never resolves it, so nothing in it is a template any more — and a
    # hand-typed `/render?path=~/.fused-render/templates/claude/template.html`
    # would otherwise still serve the retired iframe page off disk.
    user_root = fold(os.path.realpath(USER_TEMPLATES_DIR))
    if fold(p).startswith(user_root + os.sep):
        name = os.path.relpath(fold(p), user_root).split(os.sep)[0]
        if _native_folder(os.path.join(TEMPLATES_DIR, name)):
            return True
    folder = (os.path.dirname(p)
              if fold(os.path.basename(p)) == NATIVE_MARKER else p)
    from fused_render.core_templates import PACKAGE_TEMPLATES_DIR
    bases = (USER_TEMPLATES_DIR, TEMPLATES_DIR, PACKAGE_TEMPLATES_DIR)
    roots = {fold(os.path.realpath(r)) for r in bases}
    if fold(os.path.dirname(folder)) not in roots:
        return False
    return _native_folder(folder)


def _icon_for(template_path: str):
    """abs icon.svg beside the resolved template.html, or None (SPEC PT-11)."""
    icon = os.path.join(os.path.dirname(template_path), "icon.svg")
    return icon if os.path.isfile(icon) else None


def _condition_file(template_path: str):
    """The template folder's `condition.py` path, or None when it has no gate.

    A template folder may ship a `condition.py` defining `def main(path):
    bool` — the gate that decides whether the template shows for a given file
    (SPEC CT-12). No file -> the template is unconditional (the common case).
    Split from evaluation so `_apply_conditions` can cheaply tell which entries
    need running before paying to load any code.
    """
    condition_file = os.path.join(os.path.dirname(template_path), "condition.py")
    return condition_file if os.path.isfile(condition_file) else None


def _run_condition(condition_file: str, target_path: str):
    """Load+exec a `condition.py` and call `main(target_path)`. Returns
    (allowed: bool, error: str|None).

    The module is loaded fresh per call (like the registries, so an edit applies
    on the next stat with no restart) and never inserted into `sys.modules` — so
    concurrent calls with the fixed spec name get independent module objects and
    are safe to run in parallel (same rationale as executor._run_in_process). A
    broken condition — no callable `main`, or any raised exception — drops the
    template and surfaces the reason as `template_error`, mirroring how an
    unresolvable name is dropped (SPEC CT-6): a template gated by code that
    can't decide is not silently shown.
    """
    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location(
            "__fused_condition__", condition_file
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "main", None)
        if not callable(fn):
            return False, f"{condition_file}: does not define a callable 'main'"
        return bool(fn(target_path)), None
    except BaseException as e:  # never let a bad condition tear down the stat
        return False, f"{condition_file}: {e}"


def _mark_conditions(entries: list):
    """Flag resolved template entries whose folder carries a `condition.py`
    gate with `"conditional": True` (SPEC PT-8/CT-12). Sentinel entries
    (`path is None`, D73) and folders with no gate are left untouched.

    Stat no longer *evaluates* gates — a gate may do real I/O (the H3 gate
    reads a parquet footer), and that stalled every stat
    of the extension. Marking is just an isfile() per entry (~1µs); the client
    renders unconditional templates immediately and resolves the marked ones
    in the background via /api/fs/conditions. A conditional entry is never the
    client's default when an unconditional one exists.
    """
    for entry in entries:
        path = entry.get("path")
        if path is not None and _condition_file(path) is not None:
            entry["conditional"] = True


def _evaluate_conditions(gated: list, target_path: str):
    """Evaluate `condition.py` gates: `gated` is [(key, condition_file)];
    returns {key: (allowed: bool, error: str|None)}.

    Gates are independent and may be slow (user code — filesystem reads,
    remote I/O), so they are evaluated **concurrently**: the cost is the
    slowest single gate, not their sum. Results are keyed, so ordering and
    error precedence are the caller's, unaffected by completion order.
    """
    results = {}  # key -> (allowed, error)

    def _serial():
        for k, cf in gated:
            results[k] = _run_condition(cf, target_path)

    if len(gated) == 1:
        _serial()
    elif gated:
        # Bounded fan-out — an extension has at most a handful of conditional
        # templates (SPEC CT-12), so one worker per gate is fine. The pool
        # machinery itself (thread creation, submit, result) lives OUTSIDE
        # _run_condition's catch-all, so an OS refusing a new thread under load
        # would otherwise escape and 500 the request — breaking the fail-closed
        # guarantee. Contain it: on any pool failure, fall back to serial
        # evaluation, which is wholly inside _run_condition's catch-all.
        try:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=len(gated)) as pool:
                futures = {pool.submit(_run_condition, cf, target_path): k for k, cf in gated}
                for fut, k in futures.items():
                    results[k] = fut.result()
        except BaseException:
            results.clear()  # drop any partial results, re-evaluate cleanly
            _serial()

    return results


def _conditions_payload(path: str):
    """The /api/fs/conditions shape: resolve the path's templates, evaluate
    only the gated ones, and report {"conditions": {mode: bool}, "error"}.

    This is the deferred half of SPEC CT-12: stat marks gated entries
    `conditional` without running them; the client calls this endpoint in the
    background while the first unconditional template already renders. `error`
    carries the first gate error in list order (a broken gate reports False —
    fail closed — with the reason), matching stat's `template_error` posture.
    """
    try:
        st = os.stat(path)
    except OSError:
        return _error(f"no such file or directory: {path}", status=404)
    is_dir = stat_mod.S_ISDIR(st.st_mode)
    entries, _ = _templates_for(path, is_dir)

    gated = []  # [(mode, condition_file)] — mode keys are unique per list
    for entry in entries:
        if entry.get("conditional"):
            cf = _condition_file(entry["path"])
            if cf is not None:
                gated.append((entry["mode"], cf))

    results = _evaluate_conditions(gated, path)
    conditions, error = {}, None
    for mode, _cf in gated:
        allowed, err = results[mode]
        conditions[mode] = allowed
        if err and error is None:
            error = err

    payload = {"path": path, "conditions": conditions}
    if error:
        payload["error"] = error
    return payload


def _resolve_mode_list(names):
    """Resolve an ordered list of template names into `templates` stat
    entries (SPEC PT-8). Per-entry validation (SPEC CT-6): a name that can't
    resolve is dropped; `error` is the first dropped name's message.

    A known sentinel (SPEC PT-12, `KNOWN_SENTINELS`) is emitted as
    `{"mode": name, "path": None, "icon": None}` without touching the
    filesystem — referenceable from the built-in and the user registry alike
    (D73). Any other `_`-prefixed name falls through to `_resolve_name`,
    which rejects it: the rest of the sentinel namespace stays shell-owned
    (CT-6).
    """
    entries = []
    error = None
    for name in names:
        if name in KNOWN_SENTINELS:
            entries.append({"mode": name, "path": None, "icon": None})
            continue
        path, err = _resolve_name(name)
        if path is None:
            if error is None:
                error = err
            continue
        entry = {"mode": name, "path": path, "icon": _icon_for(path)}
        if _is_native(path):
            entry["native"] = True
        entries.append(entry)
    return entries, error


def _load_registry(path: str, label: str):
    """Read one registry file → (dict | None, error | None). Missing file is
    a clean no-op (SPEC CT-5). Read per call: a tiny local file, and it makes
    registry edits apply on the next stat with no restart and no cache to
    invalidate — the built-in registry rides the same loader (D73), which
    also gives editable installs live edits for free. `label` distinguishes
    the two files in errors (both basenames are registry.json).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, f"cannot read {label}: {e}"
    if not isinstance(registry, dict):
        return None, f"{label} must be a JSON object"
    return registry, None


def _key_segments(key, is_dir: bool):
    """Parse a registry key into its match segments, or None when the key
    cannot apply to this stat. Keys are dot-anchored suffix patterns (SPEC
    CT-3): ".csv", compound ".xyz.json", wildcard ".*.json" — `*` matches
    exactly one whole dot-segment, partial wildcards (".geo*") are invalid. A
    trailing "/" marks a directory key (".zarr/", D73); dir keys match only
    directories, others only files. The bare "/" is the universal directory
    key (D81): zero segments, matches any directory — returned as `[]`
    (distinct from None), ranked lowest by `_match_registry`. A key of the
    wrong shape (no leading dot, empty segment) never matches — same
    silent-ignore the no-leading-dot rule always had.
    """
    key = str(key).lower()
    dir_key = key.endswith("/")
    if dir_key != is_dir:
        return None
    if dir_key:
        key = key[:-1]
        if key == "":
            return []  # universal directory key ("/"): matches any directory
    if not key.startswith(".") or len(key) < 2:
        return None
    segs = key[1:].split(".")
    for seg in segs:
        if not seg or ("*" in seg and seg != "*"):
            return None
    return segs


def _match_registry(registry: dict, basename: str, is_dir: bool):
    """Best-matching (key, value) for basename against registry keys, or
    None. Longest-suffix semantics generalized to patterns (SPEC CT-3, D73):
    a key with more segments beats one with fewer; at equal length, comparing
    from the rightmost segment, a literal beats a `*` (`.xyz.json` >
    `.*.json` > `.json`). The universal `/` directory key (zero segments, D81)
    ranks below every dot-anchored key (`.zarr/` > `/`) and its stem is the
    whole basename. A match needs a non-empty stem before the matched suffix,
    so a dotfile named exactly like a key (a file literally called ".json")
    does not match. Case-insensitive throughout.
    """
    fsegs = basename.lower().split(".")
    best = None  # (n_segments, literal-mask right-to-left, key, value)
    for key, value in registry.items():
        ksegs = _key_segments(key, is_dir)
        if ksegs is None:
            continue
        n = len(ksegs)
        if n == 0:
            # Universal directory key: matches any directory (stem = whole
            # basename, non-empty), lowest specificity so any real key wins.
            rank = (0, ())
        else:
            if len(fsegs) <= n:
                continue
            if not ".".join(fsegs[:-n]):
                continue
            tail = fsegs[-n:]
            if any(not (k == f or (k == "*" and f)) for k, f in zip(ksegs, tail)):
                continue
            rank = (n, tuple(s != "*" for s in reversed(ksegs)))
        if best is None or rank > best[0]:
            best = (rank, key, value)
    if best is None:
        return None
    return best[1], best[2]


def _names_from_value(key, value, builtin_names: list):
    """Interpret one matched registry value (SPEC CT-2/CT-10/CT-11).

    Returns (names, disabled, error). names: ordered list[str] of (possibly
    still-unresolved) template names, or None when the value disables previews.
    disabled: True for `null` **and for an empty list** (`[]`) — both mean "no
    template at all for this type", no error, no built-in fallback. error: a
    shape-level problem (value not list/string/null) — surfaced as
    `template_error` so typos aren't silent.

    There is no `"..."` splice: the token is treated as an ordinary name that
    resolves to no folder (a dangling ref, surfaced broken), not a splice into
    the built-in list. `builtin_names` is unused, kept for signature stability.
    """
    if value is None:
        return None, True, None
    if isinstance(value, str):
        # String = exactly a single-mode list (D50).
        return [value], False, None
    if isinstance(value, list):
        # Empty list disables previews, identical to `null` (owner 2026-07-09).
        if not value:
            return None, True, None
        # Names pass through verbatim; any that resolve to no folder are kept
        # and surfaced as broken (dangling refs), never spliced or expanded.
        return list(value), False, None
    return None, False, f"{key}: registry value must be a list, string, or null"


_TEXT_SNIFF_BYTES = 8192


def _looks_like_text(path: str) -> bool:
    """Best-effort "is this a text file" sniff for the no-binding fallback.

    Reads a small prefix: a NUL byte means binary; otherwise the prefix must
    decode as UTF-8 (the encoding the text/code viewers assume). Decoding is
    incremental with ``final=False`` so a multibyte char split by the read
    boundary isn't mistaken for binary. Any read error (permission, gone, not a
    regular file) -> False, so the caller keeps the metadata card. An empty
    file counts as text (harmless to open in the viewer).
    """
    try:
        with open(path, "rb") as f:
            chunk = f.read(_TEXT_SNIFF_BYTES)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(chunk, final=False)
    except UnicodeDecodeError:
        return False
    return True


def _templates_for(path: str, is_dir: bool):
    """Returns (templates: list[dict], template_error: str|None) — SPEC PT-8.

    Both binding tables are registries in one format (D73): the built-in
    templates/registry.json and the user ~/.fused-render/templates/registry.json, both
    resolved by `_match_registry` — dot-anchored suffix patterns with `*`
    wildcard segments and trailing-"/" directory keys. Directories therefore
    resolve exactly like files (a `.zarr` store matches the ".zarr/" key),
    and the user registry binds them too (D73 revises D65). Precedence: any
    user match > built-in match (CT-3). .html/.htm are ordinary keys (D73
    revises CT-4): the user can rebind them, listing `_render` explicitly to
    keep it reachable. A path with no match in either registry returns empty —
    unmapped file, or the plain listing view for a directory.
    """
    basename = os.path.basename(os.path.normpath(path))

    builtin_names = []
    builtin_reg, error = _load_registry(BUILTIN_REGISTRY, "built-in registry.json")
    if builtin_reg is not None:
        matched = _match_registry(builtin_reg, basename, is_dir)
        if matched is not None:
            names, disabled, err = _names_from_value(*matched, builtin_names=[])
            error = error or err
            if names and not disabled:
                builtin_names = names

    user_names, disabled = None, False
    user_reg, user_err = _load_registry(USER_REGISTRY, "registry.json")
    if user_reg is not None:
        matched = _match_registry(user_reg, basename, is_dir)
        if matched is not None:
            user_names, disabled, err = _names_from_value(*matched, builtin_names)
            user_err = user_err or err
    error = error or user_err

    if disabled:
        # The user explicitly bound this key to null (CT-2) — honor "no
        # template" and never second-guess it with the text sniff below.
        return [], error

    if user_names is None:
        # No user binding, or a parse/shape-level problem — either way fall
        # back to the built-in list (CT-6); `error` carries the problem.
        entries, entry_err = _resolve_mode_list(builtin_names)
        error = error or entry_err
    else:
        entries, entry_err = _resolve_mode_list(user_names)
        error = error or entry_err
        if not entries:
            # The user's value resolved to nothing at all -> built-in fallback.
            entries, _ = _resolve_mode_list(builtin_names)

    if not entries and not is_dir and _looks_like_text(path):
        # Nothing in either registry matched. Many config/dotfiles are plain
        # text the suffix matcher structurally can't reach — its keys are
        # dot-anchored *suffixes* needing a non-empty stem, so a whole-name
        # dotfile (".gitignore", ".gitconfig", ".npmrc") never matches, and
        # extensionless files ("Makefile", "LICENSE") have no suffix at all.
        # Rather than the bare metadata card, sniff the bytes and, when they're
        # text, offer the code viewer — it renders the same bytes as `text` but
        # with syntax highlighting, line numbers and an editor, so it is the only
        # viewer worth offering here. Binary keeps the metadata fallback (empty
        # list).
        entries, _ = _resolve_mode_list(["code"])

    # Conditional templates (SPEC PT-8): a template folder may gate itself on
    # the file with a `condition.py`. Mark after resolution so gating is
    # orthogonal to the registry — it applies to whatever list survived,
    # built-in or user, main path or text-sniff fallback. Evaluation is
    # deferred to /api/fs/conditions so a slow gate never stalls the stat.
    _mark_conditions(entries)
    return entries, error
