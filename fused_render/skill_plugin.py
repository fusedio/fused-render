"""Assemble the canonical fused-render skills into a Claude Code **plugin root**
that fused-render owns outright, under ``home_dir()/skill-plugin/`` (D212).

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
import subprocess

from fused_render.shell.storage import home_dir

logger = logging.getLogger(__name__)

# The skills that go in the plugin — all three, same set as the user-level sync
# (a session launched by us has as much use for usage guidance as for authoring
# guidance). `tests/test_skill_plugin.py` pins this against user_skills.SKILLS
# and against the real repo dirs, so the two lists cannot drift apart.
SKILLS = (
    "fused-render-authoring",
    "fused-render-custom-templates",
    "fused-render-usage",
)

# The assembled root's name under home_dir(), and the shape the CLI's plugin
# loader requires inside it. Named constants because both the templates that
# pass `--plugin-dir` (templates/claude*/agent.py, which cannot import this
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

# Whether a given claude binary understands `--plugin-dir`, cached per binary
# for the life of the process. The probe lives HERE, on the server side, and not
# in the templates that pass the flag: it is one `claude --help` (~0.1s) at
# startup instead of a subprocess on every turn, and a template's `_start` stays
# free of shelling out.
_PLUGIN_DIR_SUPPORT = {}

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


def _is_loadable(root: str) -> bool:
    """Whether `root` is a plugin the CLI would actually load — i.e. the
    manifest is there. The caller returns None instead of a path that would make
    `claude --plugin-dir` fail on a tree we half-wrote."""
    return os.path.isfile(os.path.join(root, MANIFEST_DIR, MANIFEST_NAME))


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

    try:
        stamp = _fingerprint(sources)
        stamp_path = _stamp_path()
        if _is_loadable(root):
            try:
                with open(stamp_path, encoding="utf-8") as fh:
                    if fh.read() == stamp:
                        return root
            except OSError:
                pass  # no stamp (or unreadable): rebuild

        staging = root + ".new"
        _build(staging, sources)
        # Delete-then-rename, not a rename over the top: os.replace refuses a
        # non-empty destination directory on POSIX, and there is no atomic
        # directory swap to have instead. The window is a few milliseconds and
        # the only reader is a `claude` process starting up, which either sees
        # the old complete tree or the new one.
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(os.path.dirname(root), exist_ok=True)
        os.replace(staging, root)
        with open(stamp_path, "w", encoding="utf-8") as fh:
            fh.write(stamp)
        return root
    except OSError as exc:
        logger.warning("could not assemble the skill plugin at %s: %s", root, exc)
        shutil.rmtree(root + ".new", ignore_errors=True)
        return root if _is_loadable(root) else None


# ------------------------------------------- handing the root to the sessions

def _claude_bin() -> str | None:
    """The claude executable to probe, or None when we cannot find one.

    Deliberately only the two cheap answers — the explicit override and PATH.
    The templates additionally search the platform's known install locations
    (a Windows GUI-launched app has a stale PATH), and duplicating that list
    here would be a second copy to keep in step for the sake of a probe whose
    "unknown" answer is already safe (see `export_skill_plugin_env`).
    """
    override = os.environ.get("FUSED_RENDER_CLAUDE_BIN")
    if override and os.path.isfile(override):
        return override
    return shutil.which("claude")


def _supports_plugin_dir(binary: str) -> bool:
    """Whether `binary` accepts `--plugin-dir`, by reading its own `--help`.

    Cached per binary. Both streams are searched: a shim that prints usage on
    stderr is not an unsupported CLI. Any failure to run it at all answers
    False, which `export_skill_plugin_env` then reads as "cannot tell".
    """
    cached = _PLUGIN_DIR_SUPPORT.get(binary)
    if cached is None:
        try:
            out = subprocess.run([binary, "--help"], capture_output=True,
                                 text=True, timeout=30)
            cached = "--plugin-dir" in ((out.stdout or "") + (out.stderr or ""))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.debug("could not probe %s for --plugin-dir: %s", binary, exc)
            cached = False
        _PLUGIN_DIR_SUPPORT[binary] = cached
    return cached


def export_skill_plugin_env() -> str | None:
    """Sync the plugin root and publish it for the sessions we spawn; returns the
    exported path, or None when the var was cleared.

    Called from `server.export_app_env`, i.e. once before the server serves, so
    every child inherits it — and so the `--help` probe happens there instead of
    on every turn. `templates/shared/appenv.py:skill_plugin_dir` is the only
    reader; a template that finds it unset simply passes no flag.

    The flag is withheld only when we have positive evidence the CLI cannot take
    it: a `claude` we FOUND whose help does not list `--plugin-dir`. A binary we
    could not find at all still gets the flag, because "not on the server's PATH"
    is the normal state on Windows (the templates look in the install locations
    the server does not) — treating that as unsupported would silently withhold
    the skills from exactly the users least likely to notice.
    """
    root = sync_skill_plugin()
    if root is None:
        os.environ.pop(PLUGIN_DIR_ENV, None)
        return None
    binary = _claude_bin()
    if binary is not None and not _supports_plugin_dir(binary):
        # An unknown option makes the CLI exit before the turn starts, so this
        # fails CLOSED: an older install loses the skills, not every chat.
        logger.info("%s does not support --plugin-dir; the fused-render skills "
                    "will only be available via the user-level skills dir",
                    binary)
        os.environ.pop(PLUGIN_DIR_ENV, None)
        return None
    os.environ[PLUGIN_DIR_ENV] = root
    return root
