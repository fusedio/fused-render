"""Gate for the zarr_aoi template (SPEC CT-12).

`main(path)` returns True only when `path` is (or should be previewed as) a
Zarr store, so the AOI streamer stops being offered on every directory. It runs
on EVERY directory the user opens — including slow/large remote mounts — so
efficiency is the whole design, and the gate never does more I/O than it must:

1. **Zero-I/O name fast path.** A directory whose basename ends (case-
   insensitively) with `.zarr` is a store by convention; that's the common
   `foo.zarr` case (which reached the template via the `.zarr/` registry key).
   Decided True with NO filesystem calls at all.

2. **Bounded, short-circuiting marker probes.** For a directory that isn't
   `.zarr`-named, look for a small fixed set of store-marker files INSIDE it
   with targeted `os.path.isfile(join(path, marker))` calls, returning True on
   the FIRST hit. Order is cheapest/most-likely first: `.zmetadata`
   (consolidated metadata — the common cloud case), `zarr.json` (v3 group),
   `.zgroup` (v2 group). Each probe is constant-time regardless of how many
   entries the store holds.

   Only GROUP roots are matched, not bare arrays (`.zarray`): `zarr_aoi` opens
   the store with `zarr.open_group()` (`tile_server.py`), which raises on an
   array root, so offering the template there would only produce an error
   overlay. A world-scale store is a group in practice; a rare top-level bare
   array is deliberately not offered rather than offered-then-broken.

CRITICAL: this never lists or walks the directory (`os.listdir`, `os.scandir`,
`glob`, recursion). On a world-scale remote store a listing scales with entry
count and blows past the mount's timeout — the exact failure this gate exists to
avoid; a targeted `isfile`/HEAD stays constant-time.

Fails closed: any exception while probing returns False (the template is dropped
quietly), and a path that isn't a directory (and isn't `.zarr`-suffixed) is
False. Self-contained — the module is exec'd standalone (not imported as part of
a package), so it imports only stdlib.
"""

# Zarr GROUP roots only (v3 group / v2 group / consolidated) — cheapest/most-
# likely first. `.zarray` is intentionally excluded: zarr_aoi renders groups.
_STORE_MARKERS = (".zmetadata", "zarr.json", ".zgroup")


def main(path: str) -> bool:
    import os

    try:
        # Zero-I/O name fast path: strip any trailing slash, then a case-
        # insensitive `.zarr` suffix decides True with no filesystem calls.
        # This does NOT verify the path is a directory (that would cost a stat
        # and defeat the fast path). Safe because the gate only ever runs on
        # entries the registry matched, and both zarr keys (`.zarr/`, `/`) are
        # directory-only — a `.zarr`-named *file* never reaches this condition.
        name = os.path.basename((path or "").rstrip("/"))
        if name.lower().endswith(".zarr"):
            return True

        # Otherwise it must be a directory carrying a store marker. Bail early
        # (no marker probes) if it isn't even a directory.
        if not os.path.isdir(path):
            return False

        # Bounded, short-circuiting probes — first hit wins, never a listing.
        return any(os.path.isfile(os.path.join(path, m)) for m in _STORE_MARKERS)
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
