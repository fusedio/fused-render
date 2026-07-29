"""Gate for the folder-level `graph` template (SPEC CT-12, §32).

`main(path)` decides whether a directory is offered the link-graph mode. Two
questions, in this order, because the first one is a refusal rather than a
preference:

1. **Is the path mount-backed?** Then False, always. The graph's whole job is a
   recursive walk, and a recursive walk over an rclone-NFS mount is the exact
   shape that wedges it (a kernel listing on a flat million-key S3 prefix). The
   mode is therefore never OFFERED on a mount — and `markdown/graph.py` also
   refuses a mount-backed root outright, so a hand-written URL cannot reach the
   walk either. The gate is the UX; the module is the guarantee (MD-11).

   The detector is the app's own `shell.mounts.is_mount_backed`, the same one
   `server._run_condition` uses to decide whether this gate needs its
   mount-routing shim at all — not a second copy of the rule. If that import
   fails we cannot tell, and "cannot tell" must read as "refuse".

2. **Does this folder look like a notes VAULT?** A link graph is a notes tool:
   it draws what the notes say about each other. `index.md` is the marker used
   to tell a vault from an ordinary code repository — a vault's entry point is
   conventionally an index note, whereas a repository's `README.md` says
   nothing about links and is present in essentially every repository there is.
   Probing README would offer the mode on every checkout on the disk, which is
   noise, so it is not probed. Two `os.path.isfile` probes, `index.md` and
   `Index.md`: only a case-INSENSITIVE filesystem answers one for the other,
   and a second stat is free.

CRITICAL: this never lists or walks the directory (`os.listdir`, `os.scandir`,
`glob`, recursion) — the rule `zarr_aoi/condition.py` documents, and doubly
binding here: this gate runs on EVERY directory the user opens, and the mode it
gates is itself a walk. A targeted `isfile` is constant-time no matter how many
entries the folder holds; a listing is proportional to them.

The cost of the marker being wrong is small and one-directional — it costs
DISCOVERABILITY, never capability: a vault with no `index.md` simply isn't
OFFERED the mode, while the local graph panel in the note view still works and
the folder mode stays reachable by adding `_mode=graph` to the URL by hand.
Offering it on every directory that happens to hold a README — or on every
directory in the filesystem, which is what a content sniff would need a listing
to avoid — is the failure worth preventing.

Fails closed: any exception while probing returns False, and a path that isn't a
directory is False. Self-contained apart from `../shared/appenv.py` (itself
stdlib-only, env vars only) for the mount check — the module is exec'd standalone
(not imported as part of a package), so nothing here imports fused_render.
"""

# The vault marker, in both casings (only a case-INSENSITIVE filesystem answers
# one for the other). Deliberately NOT README.md and friends: those live in
# every code repository, where a link graph means nothing. The set stays small
# and fixed, so the gate is constant-time.
_VAULT_INDEX = (
    "index.md",
    "Index.md",
)


def main(path: str) -> bool:
    import os
    import sys

    try:
        # (1) A mount-backed path is refused before any probe: not offered, ever.
        #
        # Through `shared/appenv` (env vars only, stdlib only) rather than by
        # importing fused_render, so the mount rule has ONE home for every
        # template — this gate happens to be exec'd in-process by
        # server._run_condition, where the package IS importable, but that is an
        # implementation detail of the gate's host and not something a template
        # may rely on (SPEC PY-15).
        shared = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
        # Guarded insert: _run_condition re-execs this module on EVERY stat, so an
        # unconditional insert would grow sys.path without bound.
        if shared not in sys.path:
            sys.path.insert(0, shared)
        try:
            from appenv import is_mount_backed
        except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
            return False
        if is_mount_backed(path):
            return False

        # One cheap stat: the `/` registry key only ever matches directories, but
        # a caller with a stale path should not reach the probes below.
        if not os.path.isdir(path):
            return False

        # (2) Bounded, short-circuiting probes — first hit wins, never a listing.
        for name in _VAULT_INDEX:
            if os.path.isfile(os.path.join(path, name)):
                return True
        return False
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
