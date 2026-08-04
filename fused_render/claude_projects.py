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

**What counts as an app inside a discovered root is decided by the folder's
SHAPE, not by the repository it lives in** — a positive test that replaced the
original filter, which skipped any root inside a git repository.

That filter was justified on one measurement and generalised badly. In this
repo's own checkout the bare two-level rule reports 7 "apps" (3 internal, 4
duplicates of the seeded examples), which looked like proof that "a checkout is
not a workspace". Run against a real project list it said the opposite: 17
roots, 14 of them checkouts, **0 apps listed**. People keep folders of little
apps inside repositories; version control says nothing about whether a
directory holds apps.

So a folder is a TAG FOLDER when most of its subdirectories are app-shaped —
`MIN_TAG_APPS` of them and `MIN_TAG_SHARE` of the total (`_tag_apps`). Measured
on the same trees that motivated the old filter: a user's sandbox 9/9 (100%),
this repo's `fused_render/` 3/8 (38%), its root 2/10 (20%). Each root is tried
at both depths — as a tag folder itself, and as a holder of tag folders —
because which folder a user opens Claude Code in is theirs to choose.

Read-only, like the Claude Science source (D205): nothing here writes to,
scaffolds into or commits to a discovered folder. Unlike that one, a discovered
app IS an ordinary Fused app — it is the same shape, in a folder the user owns
— so it opens the same way, beside a Claude chat.
"""
import json
import logging
import os

from fused_render import app_listing, claude_science

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

#: What makes a folder a TAG FOLDER rather than an ordinary directory that
#: happens to contain a page: most of its subdirectories are app-shaped. Both
#: bounds matter. The count stops a directory with one lone `index.html` child
#: from becoming a "workspace"; the share is what rejects a source tree, where
#: a handful of app-shaped dirs sit among many that are not. Calibrated on real
#: trees rather than taste — see `_tag_apps`.
MIN_TAG_APPS = 2
MIN_TAG_SHARE = 0.5

#: Never descended when scanning an unknown root. `IGNORED_CHILDREN` covers
#: what is never an app; this adds the directories that are merely EXPENSIVE to
#: list — a `node_modules` alone can be tens of thousands of entries, and this
#: source walks repositories it knows nothing about, on every Home render.
#: Mirrors `server/walk.WALK_IGNORE_DIRS` (SR-2a) without importing the server
#: layer into a top-level module.
SKIP_DIRS = frozenset(app_listing.IGNORED_CHILDREN | {
    "node_modules", "venv", ".venv", "site-packages", "dist", "build", "target",
})


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


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base + os.sep)


def _is_workspace_like(root: str, exclude: str) -> bool:
    """Whether `root` should be scanned for apps at all.

    Two refusals, in cost order:

    * it belongs to a source that already lists it — the workspace being listed
      (`exclude`), or the Claude Science store. Both would otherwise be walked a
      second time by a rule that does not fit them. The science store is the
      sharper case and the reason this is a list rather than one path: its own
      directory is hidden and so caught below, but a project root INSIDE it is
      not (`.../orgs/<org>/artifacts` has an ordinary basename), and the
      two-level rule reads that as `<project-id>/<artifact-uuid>/` — so an
      artifact whose newest version happens to be its only `.html` would come
      back as a `claude-code` app and open via claude_split, the exact
      version-stacked, read-only path D205 special-cases claude-science to
      avoid. Reachable only if the user has run Claude Code cwd'd inside that
      store, which is unlikely and cheap to rule out.
    * it is a hidden directory.

    What it deliberately does NOT check is whether the root is a git
    repository — see `_tag_apps` for what replaced that and why.
    """
    for owned in (exclude, claude_science.claude_science_dir()):
        if _under(root, owned):
            return False
    return not os.path.basename(root).startswith(".")


def _app_shaped(path: str) -> bool:
    """A directory holding exactly one non-hidden top-level `.html` — the shape
    of an app. Cheap: one listdir, no recursion."""
    try:
        return os.path.isdir(path) and app_listing.app_entry(path) is not None
    except OSError:
        return False


def _child_dirs(folder: str) -> list[str]:
    try:
        return [c for c in sorted(os.listdir(folder))
                if not c.startswith(".") and c not in SKIP_DIRS
                and os.path.isdir(os.path.join(folder, c))]
    except OSError:
        return []


def _tag_apps(folder: str, tag: str, source: str) -> list[dict]:
    """The apps in `folder` if it is a TAG FOLDER, else [].

    A tag folder is one whose children are mostly apps — at least
    `MIN_TAG_APPS` of them, and at least `MIN_TAG_SHARE` of its subdirectories.
    That is a positive test for what the folder is FOR, and it replaced the
    original filter, which asked whether the root was inside a git repository.

    That filter was wrong, and it is worth recording why rather than just
    deleting it. It was justified on one measurement — this repo's own
    checkout, where the bare two-level rule reports 7 "apps", 3 internal and 4
    duplicates of the seeded examples — and the inference drawn from it ("a
    checkout is not a workspace") does not survive contact with a real machine.
    Run against a user's actual project list: 17 roots, 14 of them checkouts,
    0 apps listed. People keep folders of little apps INSIDE repositories,
    which is normal and none of our business; version control says nothing
    about whether a directory holds apps.

    Density does, and it separates the same two populations that motivated the
    filter, measured on the same trees: a user's sandbox scored 9/9 (100%)
    while this repo's `fused_render/` scored 3/8 (38%) and its root 2/10 (20%).
    `examples_seed/` scores 4/5 and is accepted — correctly; those are example
    apps.
    """
    children = _child_dirs(folder)
    if len(children) < MIN_TAG_APPS:
        return []
    shaped = [c for c in children if _app_shaped(os.path.join(folder, c))]
    if len(shaped) < MIN_TAG_APPS or len(shaped) < MIN_TAG_SHARE * len(children):
        return []
    return [app_listing.app_dict(os.path.join(folder, c), c, tag, source)
            for c in shaped]


def list_apps(exclude_root: str) -> list[dict]:
    """Apps in every discovered workspace, as app dicts.

    `exclude_root` is the workspace the caller lists itself (`fused_dir()`);
    anything at or under it is left to that source so nothing is reported twice.
    Empty when Claude Code isn't installed, which is the common case and not a
    condition worth reporting.

    Each root is tried at BOTH depths, because the folder a user opens Claude
    Code in is theirs to choose and neither depth is more correct: the root
    itself may be a tag folder (`<root>/<name>/one.html`, the shape of a folder
    of little apps — the observed common case) or it may hold tag folders
    (`<root>/<tag>/<name>/one.html`, the workspace shape). `_tag_apps` decides
    each candidate on its own density, so trying both costs one extra listdir
    per root and cannot turn a rejected folder into an accepted one.

    Deduplicated by app path: two project entries can nest (a workspace and a
    folder inside it), the two depths can overlap, and the same app must not
    become two cards.
    """
    exclude = os.path.abspath(exclude_root)
    apps: list[dict] = []
    seen: set[str] = set()
    truncated = 0
    for root in project_roots():
        if not _is_workspace_like(root, exclude):
            continue
        found = _tag_apps(root, os.path.basename(root), SOURCE)
        for tag in _child_dirs(root):
            found += _tag_apps(os.path.join(root, tag), tag, SOURCE)
        for app in found:
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
