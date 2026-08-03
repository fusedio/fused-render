"""Additional Fused workspaces, discovered from Claude Code's project list.

`GET /api/apps` lists the apps in *the* workspace (`fused_dir()`,
~/Documents/Fused). A user can easily have a second folder of the same shape
somewhere else — one they keep on an external drive, one a colleague shared,
one they made before they knew where the default lived — and today Home cannot
see it. Claude Code already knows where those folders are, because the user has
run it in them.

**The source is `~/.claude.json` and nothing else** (owner's call). It carries a
`projects` object keyed by ABSOLUTE PATH — every directory Claude Code has been
run in — so no slug decoding and no transcript parsing is needed to get an
exact list. What it does not carry is a timestamp per project, so nothing here
orders anything: recency comes from the filesystem, exactly as it does for the
workspace.

**The rule for what counts as an app inside a discovered root is the workspace
rule, unchanged** (owner's call): `<root>/<tag>/<name>/` with the single
non-hidden direct-child `.html` as the entry (`app_listing.two_level_apps`, the
same function the workspace listing calls). No new notion of an app, and no
guessing at one.

That rule is only meaningful in a folder whose PURPOSE is holding apps, which
is why the load-bearing filter here is `_is_workspace_like`: **a root that is
inside a git repository is skipped**. Claude Code's project list is
overwhelmingly source checkouts, and the two-level rule finds junk in those —
measured on this repo, it reports 7 "apps", of which 3 are internal
(`app_starter`, `static`, `template_starter`) and 4 are `examples_seed/*`
duplicates of what the user already has seeded. A Fused workspace, by contrast,
is *not* a repo at its root: `init_repo` runs per app dir
(`<workspace>/<tag>/<name>`), never on the workspace itself. So "has no
repository above it" separates the two populations almost perfectly, for a
handful of stats per root. The accepted miss: a user who git-inits their whole
workspace is not discovered. Their primary workspace still lists through
`fused_dir()`, and the alternative — trusting the two-level rule inside every
checkout on the disk — is the noise this exists to avoid.

Read-only, like the Claude Science source (D205): nothing here writes to,
scaffolds into or commits to a discovered folder. Unlike that one, a discovered
app IS an ordinary Fused app — it is the same shape, in a folder the user owns
— so it opens the same way, beside a Claude chat.
"""
import json
import logging
import os

from fused_render import app_listing

logger = logging.getLogger("fused_render")

#: Override the config file location (tests, and a relocated install).
CONFIG_ENV = "FUSED_RENDER_CLAUDE_CONFIG"

#: What the listing tags these with, alongside "workspace"/"claude-science".
SOURCE = "claude-code"

#: Bounds. A project list is normally dozens of entries; these exist so a
#: pathological config (or a symlink farm) cannot turn one Home render into an
#: unbounded walk. Both log when they bite — never a silent truncation.
MAX_ROOTS = 200
MAX_APPS = 500

#: How far up to look for a `.git` before calling a root repo-free. Bounded so
#: a deep path costs a bounded number of stats; anything deeper than this from
#: a repo root is not a checkout anyone is working in.
_ANCESTOR_LIMIT = 40


def config_path() -> str | None:
    """Claude Code's `~/.claude.json`, or None when it isn't there.

    `CLAUDE_CONFIG_DIR` relocates the config *directory* (`~/.claude`), and some
    installs put the JSON inside it rather than beside it, so both are tried.
    `expanduser` on a `join` rather than a literal, for the reason
    `file_history.config_dir` documents: this package ships a `windows/` dir,
    where a hardcoded forward slash survives `expanduser` and then matches
    nothing.
    """
    override = os.environ.get(CONFIG_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    candidates = []
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        candidates.append(os.path.join(os.path.expanduser(config_dir), ".claude.json"))
    candidates.append(os.path.expanduser(os.path.join("~", ".claude.json")))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def project_roots() -> list[str]:
    """Every existing directory in Claude Code's project list, absolute.

    The file is Claude Code's live config: it is rewritten as the user works, so
    a read can land mid-write. Every failure mode — absent, truncated, not
    JSON, a `projects` that isn't an object — degrades to "no extra roots",
    which is the state this feature adds to rather than depends on.

    Entries are kept only when they still name a directory: the list accumulates
    paths and never prunes, so a checkout since deleted or moved is normal.
    """
    path = config_path()
    if path is None:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError — a torn write is expected here,
        # not exceptional, and the next listing will read a whole file.
        logger.debug("claude-code: cannot read %s", path, exc_info=True)
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return []

    roots = []
    for raw in projects:
        if not isinstance(raw, str) or not raw:
            continue
        root = os.path.abspath(os.path.expanduser(raw))
        try:
            if not os.path.isdir(root):
                continue
        except OSError:
            continue
        roots.append(root)
        if len(roots) >= MAX_ROOTS:
            logger.warning("claude-code: project list capped at %d roots", MAX_ROOTS)
            break
    return roots


def _in_git_repo(path: str) -> bool:
    """Whether `path` is inside a git repository — the filter this module turns on.

    Walks up looking for `.git`, which covers a root that is a checkout AND a
    root that is a SUBDIRECTORY of one (running Claude Code in `~/repo/service`
    is ordinary). Stats only: `git rev-parse` per root would be a subprocess per
    Home render for an answer the filesystem already has.

    `.git` is matched as either a dir or a file, since a worktree or submodule
    records it as a file pointing elsewhere — both mean "inside a repository".
    """
    current = path
    for _ in range(_ANCESTOR_LIMIT):
        try:
            if os.path.exists(os.path.join(current, ".git")):
                return True
        except OSError:
            return True  # unreadable: assume repo, i.e. skip. Quieter is safer.
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return False


def _is_workspace_like(root: str, exclude: str) -> bool:
    """Whether `root` should be scanned for apps at all.

    Three refusals, in cost order: it is the workspace already being listed (or
    inside it) — those apps are reported by the workspace source and would
    otherwise appear twice; it is a hidden directory; it is inside a git
    repository (see the module docstring — this is what keeps source checkouts
    out).
    """
    if root == exclude or root.startswith(exclude + os.sep):
        return False
    if os.path.basename(root).startswith("."):
        return False
    return not _in_git_repo(root)


def list_apps(exclude_root: str) -> list[dict]:
    """Apps in every discovered workspace, as app dicts.

    `exclude_root` is the workspace the caller lists itself (`fused_dir()`);
    anything at or under it is left to that source so nothing is reported twice.
    Empty when Claude Code isn't installed, which is the common case and not a
    condition worth reporting.

    Deduplicated by app path: two project entries can nest (a workspace and a
    folder inside it), and the same app must not become two cards.
    """
    exclude = os.path.abspath(exclude_root)
    apps: list[dict] = []
    seen: set[str] = set()
    truncated = 0
    for root in project_roots():
        if not _is_workspace_like(root, exclude):
            continue
        for app in app_listing.two_level_apps(root, SOURCE):
            if app["path"] in seen:
                continue
            if len(apps) >= MAX_APPS:
                truncated += 1
                continue
            seen.add(app["path"])
            apps.append(app)
    if truncated:
        logger.warning("claude-code: %d app(s) beyond the %d cap were not listed",
                       truncated, MAX_APPS)
    return apps
