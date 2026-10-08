"""How a template learns about the app it is running inside — via the ENV.

SPEC PY-15 / DECISIONS D166.

This module is the supported contract between the server and a template: the
server exports a handful of `FUSED_RENDER_*` variables before it starts serving
(`server.export_app_env`), every child process inherits them, and the
helpers here are the one place that knows how to read them.

Templates must NOT import `fused_render`. Under the fused local execution
backend `PYTHONPATH` is stripped from child processes, so a guarded import would
silently take its fallback branch. Environment variables survive that boundary,
so the facts travel as data instead of as an import.

Stdlib only, for the same reason — a template must stay runnable as a standalone
copy of its folder, with nothing but `../shared/` beside it.

Everything is resolved PER CALL from `os.environ`, never cached at import time:
some templates are long-lived daemons (`zarr_aoi/tile_server.py`).
"""
import os


def home_dir() -> str:
    """The app's shell home dir (`~/.fused-render`, or its per-branch nesting).

    `FUSED_RENDER_HOME_DIR` is exported by the server ALREADY BRANCH-RESOLVED —
    it is the output of `shell.storage.home_dir()`, not its input. So this
    function deliberately re-derives nothing: no branch nesting, no ref
    sanitizing. Duplicating those rules here is how the two copies drift.

    The fallback is the un-branched baseline, for a template running as a
    standalone script with no server around: `FUSED_RENDER_HOME` if set (the
    same override the app honors), else `~/.fused-render`.
    """
    home = os.environ.get("FUSED_RENDER_HOME_DIR")
    if home:
        return home
    return os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")


def workspace_dir() -> str:
    """The user's Fused workspace (~/Fused), where app folders live
    two levels down (<workspace>/<tag>/<name>).

    `FUSED_RENDER_WORKSPACE_DIR` is exported by the server ALREADY RESOLVED —
    the output of `shell.seed.fused_dir()`. The fallback mirrors that function
    for a template running standalone: the same `FUSED_RENDER_DIR` override
    tests use, else the default location.

    This module runs in template subprocesses and is stdlib-only — it CANNOT
    import fused_render — so the default is a copy that must be kept in step
    with `shell/seed.fused_dir()` by hand (D337 moved it out of ~/Documents).
    """
    d = os.environ.get("FUSED_RENDER_WORKSPACE_DIR")
    if d:
        return d
    return os.path.abspath(
        os.path.expanduser(os.environ.get("FUSED_RENDER_DIR") or "~/Fused")
    )


def origin() -> str | None:
    """The origin (`http://host:port`) the server is ACTUALLY serving on, or None
    when nothing published it. The server sets `FUSED_RENDER_ORIGIN`
    unconditionally before it starts serving, so None means "no server around" —
    the caller decides what to do (a daemon that must fetch bytes back through
    `/api/fs/raw` has nowhere to go and should say so, rather than guessing a
    default port that is wrong under any `--port` override).
    """
    return os.environ.get("FUSED_RENDER_ORIGIN") or None


def skill_plugin_dir() -> str | None:
    """The Claude Code plugin root to hand a session we spawn (`--plugin-dir`),
    or None when there is none to hand it.

    fused-render assembles the canonical skills into a plugin under its home dir
    and exports the path here (`skill_plugin.export_skill_plugin_env`, D216), so
    a chat this app launches knows the `fused` bridge contract regardless of the
    state of the user's `~/.claude`.

    The var is absent in exactly the cases where the flag must not be passed:
    no server around to have synced anything, or a sync that failed. Deciding
    that is the server's job — the answer arrives here already made, like every
    other value in this module.
    """
    return os.environ.get("FUSED_RENDER_SKILL_PLUGIN_DIR") or None


def workbench_plugin_dir() -> str | None:
    """A SECOND Claude Code plugin root to hand a CANVAS session — the
    `workbench` plugin's canvas/UDF skills — or None when there is none to hand.

    Separate from `skill_plugin_dir` because it is a separate plugin: it is not
    shipped in this wheel but cloned at runtime from the public `fusedio/skills`
    repo into a directory the app owns under `home_dir()`
    (`skill_plugin.fetch_workbench_skills`, from the canvases path — never from
    the user's own `~/.claude` plugin storage, which the app does not read and
    does not write). `--plugin-dir` is repeatable, so the two roots compose
    without merging trees. A canvas clone's CLAUDE.md names those skills
    (canvas.toml format above all), and handing them over per-run is what makes
    that true without asking the user to install anything or touching their
    global Claude config.

    This value being SET is not permission to pass it: the skills are for canvas
    clones only, so the caller must also check the target against
    `canvases_root()` — see `claude_agent/agent.py:_plugin_argv`. Absent
    means "no validated clone on disk" (never fetched, fetch failed, or git is
    missing) — no flag, and the clone's CLAUDE.md degrades to the folder's own
    conventions. Decided by the server
    (`skill_plugin.export_workbench_plugin_env`), like every other value here.
    """
    return os.environ.get("FUSED_RENDER_WORKBENCH_PLUGIN_DIR") or None


def canvases_root() -> str:
    """Where canvas clones live (`~/.fused-render/canvases`, or the flavor's own
    home) — the fact a template needs to answer "is this target a canvas
    clone?", which is what gates the workbench skills above.

    The server exports the already-resolved answer (`FUSED_RENDER_CANVASES_DIR`,
    from `canvases.canvases_root()`). Deliberately a DUPLICATED rule rather than
    an import — templates must not import `fused_render` (SPEC PY-15) — and the
    fallback is therefore character-for-character what `canvases.canvases_root()`
    (via `shell.storage.base_home_dir()`) and `_canvas_push.canvases_root()` use:
    `FUSED_RENDER_HOME` if set, else `~/.fused-render`, plus `canvases` —
    deliberately NOT through `home_dir()` above.

    That looks wrong beside every other helper in this module and is not: canvas
    clones are the one thing the app does not nest per branch, so resolving this
    through `home_dir()` (which layers branch nesting on top of
    `FUSED_RENDER_HOME_DIR`) would answer `<home>/branches/<ref>/canvases` on a
    branch build while the server kept its clones unnested, under the flavor's
    base home. The disagreement would not raise anything — `_in_canvases_root`
    would simply say "not a canvas" for a real clone, and the gate's default
    answer is to withhold the workbench skills, so the failure would be
    silent. If the server's rule ever gains branch nesting, this string moves
    with it (a test pins the two together).
    """
    override = os.environ.get("FUSED_RENDER_CANVASES_DIR")
    if override:
        return override
    base = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(base, "canvases")


def fused_cli_dir() -> str | None:
    """The dir holding the `fused` CLI wrapper the server put on the PATH the
    sessions we spawn inherit, or None when there is no CLI to offer.

    Set by `fusedcli.export_fused_cli_env` (D334) before the server serves.
    Its presence is the templates' whole answer to "can this session run
    `fused`?" — the claude template pre-allows `Bash(fused:*)` and mentions
    the CLI in its prompt exactly when this is set, so a machine without the
    CLI never gets a prompt promising a command that would fail. Like the
    skill plugin var, absent means "no server around, or nothing to hand" —
    the decision arrives here already made.
    """
    return os.environ.get("FUSED_RENDER_FUSED_CLI_DIR") or None


