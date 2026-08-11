import codecs
import json
import os
import stat as stat_mod
import time
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


# /api/fs/conditions evaluates template condition.py gates, which over a remote
# mount costs ~6.8s and was recomputed on every call. A small check-on-read TTL
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
    user = os.path.join(USER_TEMPLATES_DIR, name, "template.html")
    if os.path.isfile(user):
        return user, None
    builtin = os.path.join(TEMPLATES_DIR, name, "template.html")
    if os.path.isfile(builtin):
        return builtin, None
    return None, f"no template.html for {name!r} (looked in ~/.fused-render/templates/{name}/ and core {TEMPLATES_DIR}/{name}/)"


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


# Per-gate probe budget (SPEC CT-12 fail-closed). One condition gate evaluation
# shares this wall-clock deadline across ALL its mount probes. On a
# non-direct-capable mount each operations/stat can burn the full rc timeout
# resolving a miss (rclone lists the whole parent prefix), so a gate's serialized
# probes would otherwise stack to N * that timeout. 5s bounds a whole gate to
# roughly one slow probe; direct-capable mounts probe in ~1s and rarely reach it.
GATE_PROBE_BUDGET_S = 5.0

# One bounded direct-listing page fed to the gate seed (fix #3/#4). All zarr
# group-root markers are immediate children of the store dir, so a COMPLETE
# (untruncated) page of the dir's children answers all three marker isfile
# probes with zero extra network calls; 1000 keys comfortably covers a store
# root's immediate children in one unsigned request.
_GATE_LIST_MAX_KEYS = 1000


class _GateSeed:
    """What `_conditions_payload` already knows about the target dir, threaded
    into the gate shim so it answers isdir/isfile locally instead of reprobing.

    - `kinds`: exact path -> "dir"|"file"|"missing" (a verdict already taken by
      the endpoint's rc is_dir probe; consulted before any rc call).
    - `dir_path`: the normalized (rstrip("/")) dir the listing describes.
    - `file_children`: set of immediate FILE basenames from a COMPLETE listing,
      or None when no complete listing is available.
    - `listing_complete`: True only when the listing was NOT truncated, so an
      absent marker is provably absent (a truncated listing can't prove that).
    """

    def __init__(self, kinds=None, dir_path=None, file_children=None,
                 listing_complete=False):
        self.kinds = kinds or {}
        self.dir_path = dir_path
        self.file_children = file_children
        self.listing_complete = listing_complete


def _mount_gate_builtins(target_path: str, seed=None):
    """Custom `__builtins__` for a condition gate whose target is MOUNT-backed,
    so the gate's own filesystem primitives route through the rclone rc API
    instead of the kernel NFS mount.

    Kernel NFS is the enemy: a cold NEGATIVE os.path.isfile over an rclone-NFS
    mount is a kernel LOOKUP miss that forces rclone to LIST the whole parent S3
    prefix to resolve it (~18-24s on a world-scale store), tripping the macOS NFS
    deadman so the mount is declared dead. This is the same "route via rc, never
    the kernel" hardening api_fs_list / rc_list_dir already carry; the gate path
    never got it because gates run raw os.path against the mount.

    The gate is exec'd stdlib-only and calls os.path directly (we can't ask
    arbitrary gate code to call an rc helper), so we intercept at the os / open
    layer. `import os` inside the gate resolves through __import__, so a fake
    `os` injected into the module globals would just be overwritten by the real
    one — instead we override __import__ (and open) in the gate module's OWN
    __builtins__. That dict is built per _run_condition call on a fresh module,
    so this is thread-safe under the concurrent ThreadPoolExecutor fan-out
    (_evaluate_conditions) — NEVER a global monkeypatch of os.path.*, which would
    race across threads and the rest of the server.

    Fail-closed (SPEC CT-12): any rc error / timeout / unreachable rcd makes the
    routed call behave as the kernel exception would today (isfile/isdir/exists
    -> False, os.stat -> OSError, open -> OSError), so the gate returns False
    quietly. A mount path NEVER falls back to the kernel os.* — that reintroduces
    the wedge. Non-mount paths a gate might also touch pass straight through to
    the real os / open. `os.utime` (the model gates' atime restoration, MV-5) is
    the one WRITE a gate makes: on a mount it is dropped rather than routed —
    there is no atime there worth preserving, and the kernel SETATTR is the very
    call this shim exists to prevent.
    """
    import builtins
    import io

    from fused_render.shell import mounts

    real_os = os

    # The gate's probes run SERIALLY in this one thread; give them ONE shared
    # deadline (GATE_PROBE_BUDGET_S from now). Each probe is bounded to the budget
    # REMAINING, and once it is spent every further probe fails closed instantly
    # (isfile/isdir/exists -> False, stat -> OSError) instead of issuing another
    # slow rc call — so a hung/slow backend can't stack timeouts across a gate.
    deadline = time.monotonic() + GATE_PROBE_BUDGET_S

    def _probe_budget():
        return deadline - time.monotonic()

    def _isfile(p):
        if not mounts.is_mount_backed(p):
            return real_os.path.isfile(p)
        # A listing of the dir answers marker isfile with no network call
        # (fix #3/#4). PRESENCE in the page is conclusive even if the page was
        # TRUNCATED (the marker demonstrably exists); ABSENCE is only provable
        # from a COMPLETE (untruncated) page. So a truncated page that captured
        # the marker still short-circuits True, and only a truncated-and-absent
        # marker falls through to the rc probe below. real_os.path is the real
        # (captured) os.path.
        if (seed is not None and seed.file_children is not None
                and real_os.path.dirname(p) == seed.dir_path):
            if real_os.path.basename(p) in seed.file_children:
                return True
            if seed.listing_complete:
                return False
        # Else a verdict the endpoint already took for this exact path (fix #2).
        if seed is not None and p in seed.kinds:
            return seed.kinds[p] == "file"
        left = _probe_budget()
        if left <= 0:
            return False  # budget spent -> fail closed
        return mounts.rc_kind_for(p, timeout=left) == "file"

    def _isdir(p):
        if not mounts.is_mount_backed(p):
            return real_os.path.isdir(p)
        if seed is not None and p in seed.kinds:
            return seed.kinds[p] == "dir"  # no reprobe of the target (fix #2)
        left = _probe_budget()
        if left <= 0:
            return False
        return mounts.rc_kind_for(p, timeout=left) == "dir"

    def _exists(p):
        if not mounts.is_mount_backed(p):
            return real_os.path.exists(p)
        if seed is not None and p in seed.kinds:
            return seed.kinds[p] in ("file", "dir")
        left = _probe_budget()
        if left <= 0:
            return False
        return mounts.rc_kind_for(p, timeout=left) in ("file", "dir")

    def _stat(p, *a, **k):
        if not mounts.is_mount_backed(p):
            return real_os.stat(p, *a, **k)
        left = _probe_budget()
        if left <= 0:
            raise OSError(f"probe budget exhausted for {p}")
        return mounts.rc_stat_result(p, timeout=left)

    def _utime(p, *a, **k):
        # Restoring an atime is a LOCAL cache concern: the model gates read a
        # file and put its atime back so a gate is not what marks a model as
        # recently used (SPEC MV-5). A mount has no such atime to preserve, and
        # a kernel SETATTR on the mount is precisely the class of call this shim
        # exists to keep gates from making — so it is DROPPED, not routed. The
        # gates wrap it in try/except anyway; silence matches what they expect.
        if isinstance(p, str) and mounts.is_mount_backed(p):
            return None
        return real_os.utime(p, *a, **k)

    def _listdir(p="."):
        # A kernel listing over a mount is the mur-sst wedge; the gate is
        # forbidden from enumerating anyway (constant-time by design), so fail
        # closed rather than route a listing it should never issue.
        if mounts.is_mount_backed(p):
            raise OSError(f"listing not permitted for mount path {p} in a gate")
        return real_os.listdir(p)

    def _scandir(p="."):
        if mounts.is_mount_backed(p):
            raise OSError(f"scandir not permitted for mount path {p} in a gate")
        return real_os.scandir(p)

    class _OsPathShim:
        # Instance attrs win over __getattr__, so only these three route via rc;
        # join / basename / everything else delegate to the real os.path.
        isfile = staticmethod(_isfile)
        isdir = staticmethod(_isdir)
        exists = staticmethod(_exists)

        def __getattr__(self, name):
            return getattr(real_os.path, name)

    class _OsShim:
        path = _OsPathShim()
        stat = staticmethod(_stat)
        utime = staticmethod(_utime)
        listdir = staticmethod(_listdir)
        scandir = staticmethod(_scandir)

        def __getattr__(self, name):
            return getattr(real_os, name)

    os_shim = _OsShim()
    real_import = builtins.__import__
    real_open = open

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        # Route every import form of `os`/`os.path` to the shim. __import__'s
        # return-value contract differs by form: `import os` / `import os as o`
        # (name "os") and `import os.path` (name "os.path", empty fromlist) bind
        # the TOP package, then the import machinery walks .path off it via
        # getattr — so return os_shim. `from os import ...` (name "os", non-empty
        # fromlist) also wants the top package. Only `from os.path import x`
        # (name "os.path", non-empty fromlist) wants the SUBMODULE — return the
        # shim's path object so the names bind to shimmed functions.
        # NOTE: this covers os / os.path only. A gate reaching the mount through
        # a different stdlib module (pathlib, io, glob, ...) would still hit the
        # kernel — a known, deliberately out-of-scope escape (low likelihood;
        # the builtin gates use os).
        if name == "os":
            return os_shim
        if name == "os.path":
            return os_shim.path if fromlist else os_shim
        return real_import(name, globals, locals, fromlist, level)

    def _open(file, *args, **kwargs):
        if isinstance(file, str) and mounts.is_mount_backed(file):
            # The one bounded gate read (zarr.json node_type). Ranged HTTP read
            # over the mount's serve — never a kernel open. OSError -> the gate's
            # own except -> fail closed.
            data = mounts.rc_read_bounded(file)
            mode = args[0] if args else kwargs.get("mode", "r")
            if "b" in mode:
                return io.BytesIO(data)
            return io.StringIO(data.decode(kwargs.get("encoding") or "utf-8"))
        return real_open(file, *args, **kwargs)

    b = dict(vars(builtins))
    b["__import__"] = _import
    b["open"] = _open
    return b


def _run_condition(condition_file: str, target_path: str, seed=None):
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

    For a MOUNT-backed target the gate runs under a per-call, thread-safe shim
    (_mount_gate_builtins) that routes its os.path / os.stat / open off the
    kernel NFS mount and onto the rclone rc API — a cold negative os.path.isfile
    over a mount otherwise lists the whole S3 prefix and wedges the mount.
    Templates stay mount-agnostic; all mount-awareness lives here.
    """
    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location(
            "__fused_condition__", condition_file
        )
        mod = importlib.util.module_from_spec(spec)
        # Local import keeps shell ↛ server acyclic; resolves the attr at call
        # time so the mount routing is monkeypatchable in tests.
        from fused_render.shell.mounts import is_mount_backed
        if is_mount_backed(target_path):
            mod.__dict__["__builtins__"] = _mount_gate_builtins(target_path, seed)
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
    reads a parquet footer), and over a remote mount that stalled every stat
    of the extension. Marking is just an isfile() per entry (~1µs); the client
    renders unconditional templates immediately and resolves the marked ones
    in the background via /api/fs/conditions. A conditional entry is never the
    client's default when an unconditional one exists.
    """
    for entry in entries:
        path = entry.get("path")
        if path is not None and _condition_file(path) is not None:
            entry["conditional"] = True


def _evaluate_conditions(gated: list, target_path: str, seed=None):
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
            results[k] = _run_condition(cf, target_path, seed)

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
                futures = {pool.submit(_run_condition, cf, target_path, seed): k for k, cf in gated}
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
    from fused_render.shell.mounts import (
        direct_list_capable,
        direct_list_page,
        is_mount_backed,
        rc_kind_for,
    )

    seed = None
    if is_mount_backed(path):
        # A mount is_dir probe off the kernel (a kernel os.stat here is a
        # GETATTR that can force an S3 re-list and wedge the mount). "missing" is
        # a trustworthy 404; "indeterminate" (rcd down / rc error / probe budget
        # exhausted) must NOT 404 a path the user just opened — proceed treating
        # it as a dir, and the gates then fail closed on their own indeterminate
        # probes, so the endpoint still returns 200 with all-False conditions
        # rather than a spurious 404. The probe is bounded by GATE_PROBE_BUDGET_S
        # so a stalled non-direct-capable backend can't hang this endpoint before
        # the gates (each also budgeted) even start.
        kind = rc_kind_for(path, timeout=GATE_PROBE_BUDGET_S)
        if kind == "missing":
            return _error(f"no such file or directory: {path}", status=404)
        is_dir = kind != "file"
        # Feed a DEFINITIVE verdict to the gate shim so the gate answers its own
        # isdir(path) with no rc call instead of reprobing this exact path
        # (fix #2). An "indeterminate" kind (rcd blip / budget exhausted) is NOT
        # seeded: seeding it would make the gate's isdir return False without a
        # probe and pin a spurious all-False verdict (and the TTL cache would
        # hold it). Leaving seed=None lets the gate do its OWN probe, which may
        # recover, and otherwise fail closed on its own budget — the posture the
        # is_dir comment above describes.
        if kind in ("dir", "file"):
            seed = _GateSeed(kinds={path: kind})
    else:
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

    # Only now that we know a gate will actually consume it is the bounded
    # listing worth its network cost. For a direct-list-capable mount (anonymous
    # S3/GCS), one unsigned listing of the dir's immediate children answers all
    # three marker isfile probes locally (fix #3/#4) — the markers are always
    # immediate children, so a COMPLETE page proves each present/absent without
    # a per-marker rc probe. Fail-open: any error leaves the seed marker-less and
    # the gate falls back to today's per-marker probes (logged, not silent).
    if gated and seed is not None and is_dir and direct_list_capable(path):
        try:
            listing, next_token = direct_list_page(
                path, max_keys=_GATE_LIST_MAX_KEYS, timeout=GATE_PROBE_BUDGET_S)
            seed.file_children = {
                e["Name"] for e in listing if not e.get("IsDir")}
            seed.listing_complete = next_token is None
        except Exception:
            logger.debug("gate seed listing failed for %s; falling back to "
                         "per-marker probes", path, exc_info=True)
        seed.dir_path = path.rstrip("/")

    results = _evaluate_conditions(gated, path, seed)
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
        entries.append({"mode": name, "path": path, "icon": _icon_for(path)})
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
