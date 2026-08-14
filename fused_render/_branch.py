"""Branch-ref resolution for per-branch dev-server isolation.

Branch isolation is opt-in: it engages only when a ref is supplied explicitly.
There is no automatic git-branch detection — being on a feature branch does
nothing on its own; you set ``FUSED_RENDER_BRANCH`` (at build time to stamp a
packaged artifact, or at runtime to isolate a source/editable run).

Resolution priority (cached on first access within a process, not at import;
see ``_CACHED_REF``. Changing the env var after any consumer has imported this
module has no effect without an explicit reload):
1. ``FUSED_RENDER_BRANCH`` env var, if set (even empty string -> baseline opt-out).
2. Baked ref written at build time (``fused_render/_baked_branch.py``, gitignored).
3. "" (baseline).
"""
import hashlib
import os
import re
import sys

_MAX_LEN = 12
_BASE_PORT = 1777
_PORT_RANGE_SIZE = 1000
_PORT_OFFSET = 1788


def sanitize(ref: str) -> str:
    """Lowercase, collapse non [a-z0-9] runs to single '-', trim, truncate.

    Refs that name a default branch (``main``/``master``/``head``,
    case-insensitive) resolve to the baseline (``""``, i.e. no isolation),
    not a sanitized-and-kept ref.
    """
    if not ref:
        return ""
    lowered = ref.lower()
    if lowered in ("main", "master", "head"):
        return ""
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered)
    trimmed = collapsed.strip("-")
    return trimmed[:_MAX_LEN].rstrip("-")


def _baked_ref() -> str:
    try:
        from fused_render import _baked_branch

        return _baked_branch._BAKED_REF
    except ImportError:
        return ""


def _resolve_ref() -> str:
    if "FUSED_RENDER_BRANCH" in os.environ:
        return sanitize(os.environ["FUSED_RENDER_BRANCH"])

    baked = _baked_ref()
    if baked:
        return sanitize(baked)

    return ""


_CACHED_REF = None


def _cached_ref() -> str:
    global _CACHED_REF
    if _CACHED_REF is None:
        _CACHED_REF = _resolve_ref()
    return _CACHED_REF


def branch_ref(ref: str | None = None) -> str:
    if ref is not None:
        return sanitize(ref)
    return _cached_ref()


def branch_port(ref: str | None = None) -> int:
    r = branch_ref(ref)
    if not r:
        return _BASE_PORT
    digest = hashlib.sha1(r.encode()).hexdigest()
    return int(digest, 16) % _PORT_RANGE_SIZE + _PORT_OFFSET


def branch_suffix(ref: str | None = None) -> str:
    r = branch_ref(ref)
    return f"-{r}" if r else ""


# Per-branch data dirs nest under this segment so they don't clutter ``base``'s
# top level alongside baseline state (``~/.fused-render/branches/foo`` rather
# than ``~/.fused-render/foo``).
_BRANCHES_SUBDIR = "branches"


def branch_dir(base: str, ref: str | None = None) -> str:
    """Return the data dir under ``base`` for the active (or given) branch ref.

    Baseline (no ref) is ``base`` itself, unchanged. A ref nests two levels
    deep under ``base/branches/<ref>`` — the ``branches/`` container keeps
    per-branch dirs from mixing with baseline files at ``base``'s top level.
    """
    r = branch_ref(ref)
    return os.path.join(base, _BRANCHES_SUBDIR, r) if r else base


if __name__ == "__main__":
    field = sys.argv[1]
    if field == "ref":
        print(branch_ref())
    elif field == "port":
        print(branch_port())
    elif field == "suffix":
        print(branch_suffix())
    else:
        raise SystemExit(f"unknown field: {field}")
