"""Branch-ref resolution for per-branch dev-server isolation.

Resolution priority (cached at import time):
1. ``FUSED_RENDER_BRANCH`` env var, if set (even empty string -> baseline opt-out).
2. Baked ref written at build time (``fused_render/_baked_branch.py``, gitignored).
3. The current git branch, if running from a repo checkout.
4. "" (baseline).
"""
import hashlib
import os
import re
import subprocess
import sys

_MAX_LEN = 12
_BASE_PORT = 8765
_PORT_RANGE_SIZE = 1000
_PORT_OFFSET = 8776


def sanitize(ref: str) -> str:
    """Lowercase, collapse non [a-z0-9] runs to single '-', trim, truncate."""
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


def _git_ref() -> str:
    start = os.path.dirname(os.path.abspath(__file__))
    repo_root = None
    current = start
    while True:
        if os.path.isdir(os.path.join(current, ".git")):
            repo_root = current
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    if repo_root is None:
        return ""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=5,
        )
    except Exception:
        return ""

    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _resolve_ref() -> str:
    if "FUSED_RENDER_BRANCH" in os.environ:
        return sanitize(os.environ["FUSED_RENDER_BRANCH"])

    baked = _baked_ref()
    if baked:
        return sanitize(baked)

    git_ref = _git_ref()
    if git_ref:
        return sanitize(git_ref)

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
