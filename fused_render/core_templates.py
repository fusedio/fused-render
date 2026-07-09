"""Core (built-in) templates staged under ~/.fused-render/.core-templates.

The templates ship *inside* the package (fused_render/templates/), but the
server no longer reads them from there. On startup we copy the packaged set
into ~/.fused-render/.core-templates/ and the server + executor read every
built-in template, registry, and helper from that copy instead of from the
read-only app bundle.

Reset-on-release: the copy is version-gated. A `.version` marker records the
app version that last populated the dir; when it doesn't match the running
version (a fresh install or an upgrade) the whole dir is wiped and re-copied,
so every release ships pristine core templates. The copy is built in a sibling
`.staging.<pid>` dir (marker included) and swapped in with os.replace, so a
request handler reading the live dir never sees a half-written tree, and an
interrupted copy leaves the old dir intact + an orphan staging dir (never a
partial live dir). Two instances staging concurrently is tolerated, not locked
(single local user, D3): the loser of the swap race discards its staging copy.

This is the core-template channel; it is distinct from the *user* override
channel at ~/.fused-render/templates/ (server.USER_TEMPLATES_DIR), which is
never touched here and always shadows a core template of the same name.
"""
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


def ensure_core_templates() -> str:
    """Stage the packaged templates into the core dir if this release hasn't
    yet, and return the core dir. Idempotent and cheap on the common path (just
    reads the marker); does the full wipe+copy only when the version differs.

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

    staged_version = None
    try:
        with open(marker, encoding="utf-8") as f:
            staged_version = f.read().strip()
    except (OSError, ValueError):
        # OSError: absent / unreadable. ValueError (⊇ UnicodeDecodeError): the
        # marker holds non-UTF-8 garbage. Either way treat it as unstaged and
        # let the version mismatch below re-copy — never propagate at import.
        staged_version = None

    if staged_version != __version__:
        # Stage into a private sibling dir, then swap atomically. copytree never
        # targets the live dir, so a concurrent reader / a second instance can't
        # observe a partial tree, and the marker lands only inside a complete copy.
        staging = f"{core_dir}.staging.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(PACKAGE_TEMPLATES_DIR, staging)
        with open(_marker_path(staging), "w", encoding="utf-8") as f:
            f.write(__version__)
        shutil.rmtree(core_dir, ignore_errors=True)
        try:
            os.replace(staging, core_dir)
        except OSError:
            if os.path.isdir(core_dir):
                # Lost a swap race: another instance already put this version in
                # place, so a complete tree is live. Discard ours.
                shutil.rmtree(staging, ignore_errors=True)
            else:
                # Genuine swap failure with core_dir already wiped. Surface it
                # rather than silently returning a path to nothing (crash on
                # failure, by design); the staging copy is left for inspection.
                raise

    return core_dir
