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

2. **Does this folder look like it holds notes?** A small fixed set of
   `os.path.isfile` probes for the file a notes folder almost always has.

CRITICAL: this never lists or walks the directory (`os.listdir`, `os.scandir`,
`glob`, recursion) — the rule `zarr_aoi/condition.py` documents, and doubly
binding here: this gate runs on EVERY directory the user opens, and the mode it
gates is itself a walk. A targeted `isfile` is constant-time no matter how many
entries the folder holds; a listing is proportional to them.

The cost of the name list being wrong is small and one-directional: a notes
folder with no README/index/Home simply isn't offered the mode (the local graph
panel in the note view still works, and the folder mode is reachable by adding
`_mode=graph` by hand). Offering it on every directory in the filesystem — which
is what a content sniff would need a listing to avoid — is the failure worth
preventing.

Fails closed: any exception while probing returns False, and a path that isn't a
directory is False. Self-contained apart from the one mount import — the module
is exec'd standalone (not imported as part of a package).
"""

# The file a notes folder almost always has, cheapest/most-likely first. Both
# cases of each are probed because only a case-INSENSITIVE filesystem answers
# one for the other; the set stays small and fixed, so the gate is constant-time.
_LIKELY_NOTES = (
    "README.md",
    "readme.md",
    "index.md",
    "Index.md",
    "Home.md",
    "home.md",
    "notes.md",
    "Notes.md",
    "README.markdown",
    "index.markdown",
)


def main(path: str) -> bool:
    import os

    try:
        # (1) A mount-backed path is refused before any probe: not offered, ever.
        try:
            from fused_render.shell.mounts import is_mount_backed
        except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
            return False
        if is_mount_backed(path):
            return False

        # One cheap stat: the `/` registry key only ever matches directories, but
        # a caller with a stale path should not reach the probes below.
        if not os.path.isdir(path):
            return False

        # (2) Bounded, short-circuiting probes — first hit wins, never a listing.
        for name in _LIKELY_NOTES:
            if os.path.isfile(os.path.join(path, name)):
                return True
        return False
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
