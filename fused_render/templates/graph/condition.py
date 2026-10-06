"""Gate for the folder-level `graph` template (SPEC CT-12, §32).

`main(path)` decides whether a directory is offered the link-graph mode. The one question:

1. **Does this folder look like a notes VAULT?** A link graph is a notes tool:
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
directory is False. The module is exec'd standalone (not imported as part
of a package), so nothing here imports fused_render.
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

    try:
        # One cheap stat: the `/` registry key only ever matches directories, but
        # a caller with a stale path should not reach the probes below.
        if not os.path.isdir(path):
            return False

        # Bounded, short-circuiting probes — first hit wins, never a listing.
        for name in _VAULT_INDEX:
            if os.path.isfile(os.path.join(path, name)):
                return True
        return False
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
