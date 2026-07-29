import os
import stat as stat_mod
import time

import fused_render.server as _srv


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
    the real os / open.
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
    deadline = time.monotonic() + _srv.GATE_PROBE_BUDGET_S

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
        if path is not None and _srv._condition_file(path) is not None:
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
        kind = rc_kind_for(path, timeout=_srv.GATE_PROBE_BUDGET_S)
        if kind == "missing":
            return _srv._error(f"no such file or directory: {path}", status=404)
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
            return _srv._error(f"no such file or directory: {path}", status=404)
        is_dir = stat_mod.S_ISDIR(st.st_mode)
    entries, _ = _srv._templates_for(path, is_dir)

    gated = []  # [(mode, condition_file)] — mode keys are unique per list
    for entry in entries:
        if entry.get("conditional"):
            cf = _srv._condition_file(entry["path"])
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
                path, max_keys=_srv._GATE_LIST_MAX_KEYS, timeout=_srv.GATE_PROBE_BUDGET_S)
            seed.file_children = {
                e["Name"] for e in listing if not e.get("IsDir")}
            seed.listing_complete = next_token is None
        except Exception:
            _srv.logger.debug("gate seed listing failed for %s; falling back to "
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
