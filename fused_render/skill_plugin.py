"""Assemble the canonical fused-render skills into a Claude Code **plugin root**
that fused-render owns outright, under ``home_dir()/skill-plugin/`` (D216).

Why a plugin root and not just the user-level skills dir (``user_skills.py``,
D185): that sync is a *guess* about the machine, and every way it can miss is
silent. The dir it writes to may not be the one the CLI reads
(``CLAUDE_CONFIG_DIR`` set after we resolved it), a same-named user-authored
skill correctly makes us skip (marker ownership), the user may delete a skill
mid-session, and on a fresh install nothing guarantees the sync ran before the
first session did. In each case the session starts *without* the skill and
nothing says so — the model simply doesn't know the bridge contract and writes
an app against APIs that don't exist.

A directory plugin removes the guess. The sessions fused-render launches are
spawned by us, so we can hand the CLI the skills explicitly:
``claude --plugin-dir <root>`` loads them for that session only, from a path we
just wrote, regardless of the state of the user's ``~/.claude``. The plugin
loader wants a fixed shape::

    <root>/.claude-plugin/plugin.json     the manifest (plugin name, metadata)
    <root>/skills/<name>/SKILL.md         one dir per skill

and that shape is what this module builds. The repo root already *is* such a
root (committed ``.claude-plugin/plugin.json`` beside the committed ``skills/``,
which is what makes `claude plugin marketplace add fusedio/fused-render` work)
— but the end user almost never has the repo, so the same tree has to be
reassembled from whatever the install actually shipped.

Source resolution keeps D106's single-source rule, same order as
``user_skills.py``: the repo-level ``skills/`` + ``.claude-plugin/plugin.json``
win when resolvable (editable/dev installs — always the current truth), else the
packaged copies under ``fused_render/skills/`` that ``scripts/hatch_build.py``
writes at build time. The packaged manifest deliberately sits at
``fused_render/skills/plugin.json`` — NOT in a packaged ``.claude-plugin/``
dir — so nothing in the wheel lives under a dot-prefixed path; the dotted dir
exists only in the assembled output, where we mkdir it ourselves. (A dotted
directory in a wheel is a packaging tripwire: it depends on how the build
backend's include globs treat hidden paths, and getting it wrong drops the file
from the wheel with no error at all — a field-only failure.)

The whole module is best-effort and never raises: callers are server startup and
scaffolding, and a chat that loads without the skill is still a working chat.
"""
import json
import logging
import os
import shutil
import tempfile

from fused_render.shell.storage import home_dir

logger = logging.getLogger(__name__)

# The skills that go in the plugin — all of them, same set as the user-level sync
# (a session launched by us has as much use for usage guidance as for authoring
# guidance). `tests/test_skill_plugin.py` pins this against user_skills.SKILLS
# and against the real repo dirs, so the two lists cannot drift apart.
SKILLS = (
    "fused-render-ai",
    "fused-render-authoring",
    "fused-render-custom-templates",
    "fused-render-index",
    "fused-render-usage",
)

# The assembled root's name under home_dir(), and the shape the CLI's plugin
# loader requires inside it. Named constants because both the templates that
# pass `--plugin-dir` (templates/claude/agent.py, which cannot import this
# module — SPEC PY-15) and the tests hard-code the same strings.
PLUGIN_SUBDIR = "skill-plugin"
MANIFEST_DIR = ".claude-plugin"
MANIFEST_NAME = "plugin.json"
SKILLS_SUBDIR = "skills"

# The env var that carries the answer to the templates (SPEC PY-15): the root to
# pass to `--plugin-dir`, or absent for "don't pass the flag". Set once before
# the server serves (`export_skill_plugin_env`, from `server.export_app_env`),
# read only by `templates/shared/appenv.py:skill_plugin_dir`.
PLUGIN_DIR_ENV = "FUSED_RENDER_SKILL_PLUGIN_DIR"

# Fingerprint of the sources the current output was built from, kept BESIDE the
# plugin root rather than inside it: the root is handed to a plugin loader, and
# bookkeeping of ours has no business being in a tree something else parses.
_STAMP_SUFFIX = ".stamp.json"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_SKILLS_DIR = os.path.join(_REPO_ROOT, "skills")
_REPO_MANIFEST = os.path.join(_REPO_ROOT, MANIFEST_DIR, MANIFEST_NAME)
_PACKAGED_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")
_PACKAGED_MANIFEST = os.path.join(_PACKAGED_SKILLS_DIR, MANIFEST_NAME)

# Used only when neither source ships a manifest — see `_manifest_text`. Keeping
# a plugin loadable matters more than keeping its metadata complete: without a
# manifest the whole root is ignored and the skills go with it.
_FALLBACK_MANIFEST = {
    "name": "fused-render",
    "description": "Skills for using and authoring fused-render.",
}


def plugin_dir() -> str:
    """The assembled plugin root: ``home_dir()/skill-plugin``.

    Under the app's own home dir (branch-nested like everything else there), so
    a branch build's skills can never be loaded into a baseline build's session.
    """
    return os.path.join(home_dir(), PLUGIN_SUBDIR)


def _stamp_path() -> str:
    return plugin_dir() + _STAMP_SUFFIX


def _skill_sources() -> dict:
    """``{name: source dir}`` for every skill that has a source at all, repo
    copy winning over packaged copy per skill (D106). A skill with neither is
    absent from the mapping rather than faked — a plugin holding two of three
    skills is still worth loading."""
    out = {}
    for name in SKILLS:
        for root in (_REPO_SKILLS_DIR, _PACKAGED_SKILLS_DIR):
            src = os.path.join(root, name)
            if os.path.isdir(src):
                out[name] = src
                break
    return out


def _manifest_source() -> str | None:
    """The manifest to copy, repo before packaged, or None if neither is
    there."""
    for path in (_REPO_MANIFEST, _PACKAGED_MANIFEST):
        if os.path.isfile(path):
            return path
    return None


def _manifest_text() -> str:
    """The manifest bytes to write. A real one is copied verbatim (so the
    published plugin's name and metadata are the single source); a synthesized
    minimum is the fallback, loudly, because reaching it means the build did not
    ship the file it was supposed to."""
    path = _manifest_source()
    if path is not None:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            logger.warning("could not read plugin manifest %s: %s", path, exc)
    else:
        logger.warning(
            "no %s/%s found in the repo or the installed package — the skill "
            "plugin gets a synthesized minimal manifest",
            MANIFEST_DIR, MANIFEST_NAME,
        )
    return json.dumps(_FALLBACK_MANIFEST, indent=2) + "\n"


def _fingerprint(sources: dict) -> str:
    """What the output was built from, as a comparable string: every source
    file's relative path, size and mtime, plus the manifest's.

    Cheap enough to run on every server start (a handful of markdown files) and
    it is what keeps the sync from rewriting the tree each time — the swap below
    deletes and recreates the root, and doing that under a session that is
    reading it is worth avoiding when nothing changed.
    """
    entries = []
    manifest = _manifest_source()
    for label, root in [("manifest", manifest)] + sorted(sources.items()):
        if root is None:
            entries.append([label, None])
            continue
        if os.path.isfile(root):
            st = os.stat(root)
            entries.append([label, "", st.st_size, int(st.st_mtime)])
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append([label, rel, st.st_size, int(st.st_mtime)])
    return json.dumps(entries, sort_keys=True)


def _is_loadable(root: str, expected=()) -> bool:
    """Whether `root` is a plugin the CLI would actually load. The caller returns
    None instead of a path that would make `claude --plugin-dir` fail on a tree
    we half-wrote.

    `expected` names the skills that must ALSO be present, and exists because the
    manifest alone is not evidence of a complete tree: a build interrupted between
    writing the manifest and copying the skills leaves a root that loads fine and
    teaches the model nothing. Pass it wherever a "yes" would be trusted — above
    all before the stamp short-circuit, which would otherwise keep handing out a
    gutted plugin until the sources happened to change again. Left empty on the
    failure paths on purpose: there, a partial tree is being compared against
    nothing at all, and some skills beat none.
    """
    if not os.path.isfile(os.path.join(root, MANIFEST_DIR, MANIFEST_NAME)):
        return False
    for name in expected:
        if not os.path.isfile(
                os.path.join(root, SKILLS_SUBDIR, name, "SKILL.md")):
            return False
    return True


# -- the WORKBENCH plugin (canvas/UDF skills, a separate plugin we do not own) --
#
# A canvas clone's CLAUDE.md points the session at the canvas.toml format
# reference and friends. Those skills live in the `workbench` plugin from the
# `fusedio/claude-plugins` marketplace — a different plugin from the one this
# module assembles, published by the workbench team, not shipped in this wheel.
#
# Until now the clone's CLAUDE.md handled a missing plugin by telling the USER to
# run a shell command. That is wrong twice over: the reader is a Claude session
# in a chat pane, which cannot act on it, and a user reading it there is the
# wrong person to hand a terminal command to. So the app hands the skills to the
# session itself, per-run, over the same `--plugin-dir` mechanism it already uses
# for its own — session-scoped, additive, and no mutation of the user's global
# Claude config. When the plugin is nowhere on the machine the CLAUDE.md simply
# degrades and says to follow the folder's own conventions; it never instructs.
#
# `--plugin-dir` is repeatable, which is what makes composing the two roots
# possible at all rather than having to merge trees.
WORKBENCH_PLUGIN_NAME = "workbench"

# Publishes the discovered root to the templates, exactly like PLUGIN_DIR_ENV.
# Absent means "nothing to hand" and the template passes no second flag.
WORKBENCH_PLUGIN_DIR_ENV = "FUSED_RENDER_WORKBENCH_PLUGIN_DIR"

# Explicit override, checked first: a dev (or a future install that ships these
# skills somewhere of its own) can point straight at a plugin root. Distinct from
# the export var above so the export never reads its own output.
WORKBENCH_PLUGIN_SRC_ENV = "FUSED_RENDER_WORKBENCH_PLUGIN"

# The skills a clone session is actually told to load — the evidence that a
# candidate root is the plugin we mean and not a same-named stub.
WORKBENCH_SKILLS = ("canvas-toml", "fused-udfs", "json-ui-schemas", "fused-cli",
                    "canvas-comments")


def _claude_config_dir() -> str:
    """Claude Code's config dir — CLAUDE_CONFIG_DIR wins, as it does for the CLI
    itself. Read per call, never cached: user_skills.py's D185 lesson is that
    resolving this once at import is how we end up writing to a directory the
    CLI does not read."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _workbench_candidates() -> list:
    """Where a `workbench` plugin root may sit, best first.

    Both locations are Claude Code's own plugin storage, whose layout is not a
    published contract — so this is a best-effort LOOKUP, never a requirement:
    every caller treats "not found" as a normal outcome.

      marketplaces/<marketplace>/<plugin>/  the marketplace checkout. Preferred:
                                            it is a plain clone with a stable
                                            path.
      cache/<marketplace>/<plugin>/<ver>/   the installed copy, keyed by a
                                            version hash that changes on every
                                            update — usable, but only ever as
                                            the fallback, newest first.
    """
    plugins = os.path.join(_claude_config_dir(), "plugins")
    found = []
    for kind in ("marketplaces", "cache"):
        base = os.path.join(plugins, kind)
        try:
            markets = sorted(os.listdir(base))
        except OSError:
            continue
        for market in markets:
            root = os.path.join(base, market, WORKBENCH_PLUGIN_NAME)
            if kind == "marketplaces":
                found.append(root)
                continue
            try:
                versions = sorted(os.listdir(root), reverse=True)
            except OSError:
                continue
            found.extend(os.path.join(root, v) for v in versions)
    return found


def find_workbench_plugin() -> str | None:
    """A loadable `workbench` plugin root on this machine, or None.

    Validated with `_is_loadable` against WORKBENCH_SKILLS, not just the
    manifest: handing `--plugin-dir` a tree that is missing the very skills the
    CLAUDE.md names would load cleanly and teach the model nothing, which is the
    silent failure this whole mechanism exists to remove.
    """
    override = os.environ.get(WORKBENCH_PLUGIN_SRC_ENV)
    candidates = [override] if override else _workbench_candidates()
    for root in candidates:
        try:
            if _is_loadable(root, WORKBENCH_SKILLS):
                return root
        except OSError:
            continue
    return None


def export_workbench_plugin_env() -> str | None:
    """Publish the workbench plugin root for the sessions we spawn, or clear the
    var when there is none. Filesystem-only and never raises — same rules as
    `export_skill_plugin_env`, and for the same reason: this runs on the
    pre-bind startup path, where blocking is a server that failed to start."""
    try:
        root = find_workbench_plugin()
    except Exception:  # noqa: BLE001 — a lookup is never worth a failed start
        logger.warning("could not look for the workbench plugin", exc_info=True)
        root = None
    if root is None:
        os.environ.pop(WORKBENCH_PLUGIN_DIR_ENV, None)
        return None
    os.environ[WORKBENCH_PLUGIN_DIR_ENV] = root
    return root


def _build(staging: str, sources: dict) -> None:
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(os.path.join(staging, MANIFEST_DIR), exist_ok=True)
    with open(os.path.join(staging, MANIFEST_DIR, MANIFEST_NAME),
              "w", encoding="utf-8") as fh:
        fh.write(_manifest_text())
    for name, src in sources.items():
        shutil.copytree(src, os.path.join(staging, SKILLS_SUBDIR, name))


def sync_skill_plugin() -> str | None:
    """Build/refresh the plugin root and return its path, or None when there is
    nothing loadable there.

    Idempotent and quiet: unchanged sources short-circuit on the stamp, so the
    common case (every server start) touches no files. Never raises — a failure
    logs a warning and returns whatever is already on disk if that is loadable,
    because a stale-but-complete plugin beats no plugin.
    """
    root = plugin_dir()
    sources = _skill_sources()
    if not sources:
        # Neither the repo nor the package has any skill to ship. Not a
        # scenario we can repair here; leave any previous build alone.
        logger.debug("no skill sources found; leaving %s as is", root)
        return root if _is_loadable(root) else None

    staging = None
    try:
        stamp = _fingerprint(sources)
        stamp_path = _stamp_path()
        if _is_loadable(root, sources):
            try:
                with open(stamp_path, encoding="utf-8") as fh:
                    if fh.read() == stamp:
                        return root
            except OSError:
                pass  # no stamp (or unreadable): rebuild

        # A staging directory NOBODY else can be building into. A fixed
        # `<root>.new` was a real race: `export_skill_plugin_env` is called from
        # the create-app and create-template routes, which FastAPI runs on a
        # threadpool, so two scaffolds at once could each rmtree the other's
        # half-copied staging and publish whichever fragment won. mkdtemp is the
        # cheap way to be unique across both processes and threads.
        os.makedirs(os.path.dirname(root), exist_ok=True)
        staging = tempfile.mkdtemp(prefix=os.path.basename(root) + ".new-",
                                   dir=os.path.dirname(root))
        _build(staging, sources)
        # Delete-then-rename, not a rename over the top: os.replace refuses a
        # non-empty destination directory on POSIX, and there is no atomic
        # directory swap to have instead. The window is a few milliseconds and
        # the only reader is a `claude` process starting up, which either sees
        # the old complete tree or the new one.
        shutil.rmtree(root, ignore_errors=True)
        os.replace(staging, root)
        staging = None
        with open(stamp_path, "w", encoding="utf-8") as fh:
            fh.write(stamp)
        return root
    except OSError as exc:
        logger.warning("could not assemble the skill plugin at %s: %s", root, exc)
        return root if _is_loadable(root) else None
    finally:
        # Ours alone, so this cannot take a concurrent build's tree with it.
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


# ------------------------------------------- handing the root to the sessions

def export_skill_plugin_env() -> str | None:
    """Sync the plugin root and publish it for the sessions we spawn; returns the
    exported path, or None when there was nothing to publish.

    Called from `server.export_app_env`, i.e. once before the server serves, so
    every child inherits it. `templates/shared/appenv.py:skill_plugin_dir` is the
    only reader; a template that finds it unset simply passes no flag.

    Filesystem work only — no CLI capability probe. `--plugin-dir` has shipped
    since the plugin system itself (CLI 2.0.12, Oct 2025) on a binary that
    auto-updates, so an install old enough to reject the flag is not a case worth
    paying for. It was paid for: this function used to run `claude --help` here,
    on the pre-bind path, and a cold 279MB binary facing a Windows Defender
    first-touch scan took longer to answer than the desktop supervisor's whole
    20s readiness budget — killing a server that was seconds from healthy, three
    times, then showing the user a startup-failure dialog.
    """
    root = sync_skill_plugin()
    if root is None:
        os.environ.pop(PLUGIN_DIR_ENV, None)
        return None
    os.environ[PLUGIN_DIR_ENV] = root
    return root
