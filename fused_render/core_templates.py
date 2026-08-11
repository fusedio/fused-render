"""Core (built-in) templates staged under ~/.fused-render/.core-templates.

The templates ship *inside* the package (fused_render/templates/), but the
server no longer reads them from there. On startup we copy the packaged set
into ~/.fused-render/.core-templates/ and the server + executor read every
built-in template, registry, and helper from that copy instead of from the
read-only app bundle.

Reset-on-release: the copy is content-gated. A `.version` marker records
`<app version> <sha256 of the packaged tree>`; when it doesn't match the freshly
computed value (a fresh install, an upgrade, or an edited packaged template) the
whole dir is wiped and re-copied, so every release ships pristine core
templates. The digest — not the version alone — is what makes the gate honest:
version-only gating meant an edit to a packaged template inside one release was
simply never served, which is exactly how twelve retheme'd templates shipped
invisible. A marker holding a bare version string is a pre-digest install and
counts as a mismatch, so those installs heal on the next start. The copy is built in a sibling
`.staging.<pid>` dir (marker included) and swapped in with os.replace, so a
request handler reading the live dir never sees a half-written tree, and an
interrupted copy leaves the old dir intact + an orphan staging dir (never a
partial live dir). Two instances staging concurrently is tolerated, not locked
(single local user, D3): the loser of the swap race discards its staging copy.

This is the core-template channel; it is distinct from the *user* override
channel at ~/.fused-render/templates/ (server.USER_TEMPLATES_DIR), which is
never touched here and always shadows a core template of the same name.
"""
import hashlib
import os
import shutil

from fused_render import __version__
from fused_render.shell.storage import home_dir

# Source of truth: the templates shipped inside the package (app bundle).
PACKAGE_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


# Dev bypass: point this at a templates dir to read from directly, skipping the
# stage-into-home copy entirely (set it to the in-repo fused_render/templates so
# edits show up live without a version bump or a manual .core-templates wipe).
_OVERRIDE_ENV = "FUSED_RENDER_CORE_TEMPLATES"


def core_templates_dir() -> str:
    """Dest the server reads core templates from: ~/.fused-render/.core-templates.
    Resolved against home_dir() each call so FUSED_RENDER_HOME overrides work."""
    return os.path.join(home_dir(), ".core-templates")


def _marker_path(core_dir: str) -> str:
    return os.path.join(core_dir, ".version")


# __pycache__ is the ONE exclusion: template .py helpers are executed, so the
# repo/app tree can pick up byte-caches that differ between interpreters and
# runs while the served bytes are identical. Nothing under it is ever served —
# the executor imports the .py sources, not the caches. Everything else in the
# tree is in scope, including registry.json, icon.svg and template helpers.
_DIGEST_SKIP_DIRS = {"__pycache__"}


def _file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 18), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_digest(root: str) -> str:
    """sha256 over the (relative path, content) pairs of every file under `root`.

    Deterministic: dirs and files are walked in sorted order and separators are
    normalised, so the same bytes on disk always hash the same regardless of
    creation order, platform or readdir order. Content only — never mtimes or
    sizes, which change without the served bytes changing and (worse) stay put
    when they do. Each entry contributes `<relpath>\\0<file sha256>\\0`, so a
    rename is as visible as an edit and no two trees can splice into the same
    stream. A missing root hashes to the empty digest rather than raising: the
    caller's copytree will produce the real error.
    """
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _DIGEST_SKIP_DIRS)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            try:
                h.update(_file_digest(full).encode("ascii"))
            except OSError:
                # Vanished/unreadable mid-walk. Fold the fact in rather than
                # raising at import; a tree we can't fully read must not hash
                # equal to one we can.
                h.update(b"<unreadable>")
            h.update(b"\0")
    return h.hexdigest()


# Process-lifetime memo of the packaged-tree digest, keyed on PACKAGE_TEMPLATES_DIR
# so it self-corrects across the suite's per-test fake trees.
_EXPECTED_MARKER_MEMO: tuple[str, str] | None = None


def _reset_expected_marker_cache() -> None:
    """Drop the digest memo (tests only)."""
    global _EXPECTED_MARKER_MEMO
    _EXPECTED_MARKER_MEMO = None


def _expected_marker() -> str:
    """The marker a correctly staged copy of the current package would hold."""
    global _EXPECTED_MARKER_MEMO
    if _EXPECTED_MARKER_MEMO is not None and _EXPECTED_MARKER_MEMO[0] == PACKAGE_TEMPLATES_DIR:
        return _EXPECTED_MARKER_MEMO[1]
    marker = f"{__version__} {_tree_digest(PACKAGE_TEMPLATES_DIR)}"
    _EXPECTED_MARKER_MEMO = (PACKAGE_TEMPLATES_DIR, marker)
    return marker


def ensure_core_templates() -> str:
    """Stage the packaged templates into the core dir if the staged copy doesn't
    match the packaged one, and return the core dir. Idempotent and cheap on the
    common path (hash the packaged tree, read the marker, compare); does the full
    wipe+copy only when they differ.

    FUSED_RENDER_CORE_TEMPLATES short-circuits everything: the named dir is used
    verbatim with no staging, so a dev can read the in-repo templates live. It is
    abspath'd (a relative value would otherwise resolve against the process CWD,
    which changes under the app) and stripped so a whitespace-only value is
    treated as unset."""
    override = (os.environ.get(_OVERRIDE_ENV) or "").strip()
    if override:
        return os.path.abspath(override)

    core_dir = core_templates_dir()
    marker = _marker_path(core_dir)

    expected = _expected_marker()

    staged_marker = None
    try:
        with open(marker, encoding="utf-8") as f:
            staged_marker = f.read().strip()
    except (OSError, ValueError):
        # OSError: absent / unreadable. ValueError (⊇ UnicodeDecodeError): the
        # marker holds non-UTF-8 garbage. Either way treat it as unstaged and
        # let the mismatch below re-copy — never propagate at import. A marker
        # written by a pre-digest release (a bare version, no digest) lands in
        # the same mismatch branch, which is how those installs heal.
        staged_marker = None

    if staged_marker != expected:
        # Stage into a private sibling dir, then swap atomically. copytree never
        # targets the live dir, so a concurrent reader / a second instance can't
        # observe a partial tree, and the marker lands only inside a complete copy.
        staging = f"{core_dir}.staging.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(PACKAGE_TEMPLATES_DIR, staging)
        with open(_marker_path(staging), "w", encoding="utf-8") as f:
            f.write(expected)
        shutil.rmtree(core_dir, ignore_errors=True)
        try:
            os.replace(staging, core_dir)
        except OSError:
            if os.path.isdir(core_dir):
                # Lost a swap race: another instance already put this exact tree
                # in place, so a complete tree is live. Discard ours.
                shutil.rmtree(staging, ignore_errors=True)
            else:
                # Genuine swap failure with core_dir already wiped. Surface it
                # rather than silently returning a path to nothing (crash on
                # failure, by design); the staging copy is left for inspection.
                raise

    return core_dir
