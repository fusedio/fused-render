"""Claude Science artifacts, listed as apps on Home and in the /apps hub (D205).

Claude Science (Anthropic's research workbench) keeps everything it produces in
one local store, laid out like this::

    ~/.claude-science/
      orgs/<org-uuid>/
        artifacts/
          <project-id>/                     e.g. proj_f0c0cfbcfb8f, proj_example
            <artifact-uuid>/
              v<hex>_<saved-name>.<ext>     e.g. v6f4b965a_building_h3_compare.png
          .thumbnails/<2-hex-shard>/        the app's own cache
          .example_<topic>_seeded           seed markers
        operon-cli.db                       projects, artifacts, artifact_versions, …

Read that as: an **artifact is the `<artifact-uuid>` directory**, and each file
inside it is one **version** of that artifact — named ``v`` + a short hex digest
+ ``_`` + the filename the researcher's code actually saved. (Claude Science
versions an artifact when the same filename is saved again, which is exactly
what that directory holds.) The bytes are plain files — a figure is a real PNG,
a table a real CSV — so nothing here decodes or decrypts anything, and nothing
here ever writes: this module only ever reads a store another application owns.

**One artifact = one app.** That is the unit with a name a researcher would
recognise (``building_h3_compare``, not ``cd5e48e0-64e7-…``), and it is the unit
that has a single file to open — which is what makes it an app in the sense the
rest of the listing means. The project becomes the app's **tag**, so the /apps
hub's tag chips (derived from the apps themselves, no registry) turn into
per-project filters for free.

Claude Science's own **bundled sample project is skipped** (see
``EXAMPLE_PROJECT_IDS``) — it is demo content the user did not make, and on a
real store it outnumbers their own artifacts several times over.
``FUSED_RENDER_CLAUDE_SCIENCE_EXAMPLES=1`` lists it anyway.

The entry is the newest version file, whatever its type. Most Claude Science
artifacts are figures and tables rather than pages, so the listing reports
``entry`` (any previewable file, dispatched by the shell's template registry:
``.png`` → image, ``.csv`` → duckdb) alongside ``entry_html``, which stays
null unless the artifact really is an HTML page. A card with an entry that
isn't HTML is still a working card — it just previews through a different
route than the ``/render`` iframe a workspace app uses.

Three properties this module is deliberately built around, because the store
belongs to another application:

* **Never fails the listing.** Every step is guarded and skips what it cannot
  read. Home is the app's landing screen; a store we don't own must not be able
  to take it down (the caller adds one more belt at the seam).
* **Never trusts the schema.** Project display names come from ``operon-cli.db``
  — a private SQLite database whose columns are not ours to depend on — so the
  read is read-only, time-bounded, column-introspected, and falls back to the
  on-disk project id when anything about it surprises us.
* **Bounded.** The walk is exactly three levels deep with no recursion, and the
  number of artifacts it will report is capped (``MAX_ARTIFACTS``) with a log
  line when the cap bites, so a truncated listing is never silently passed off
  as a complete one.
"""
import itertools
import logging
import os
import pathlib
import re
import sqlite3

from fused_render import app_listing

logger = logging.getLogger("fused_render")

#: Overrides the store location (tests point it at a throwaway dir; a user who
#: keeps Claude Science elsewhere can point it at that).
DIR_ENV = "FUSED_RENDER_CLAUDE_SCIENCE_DIR"

DEFAULT_DIR = "~/.claude-science"

#: Set truthy to list the bundled sample project too (see `_examples_included`).
EXAMPLES_ENV = "FUSED_RENDER_CLAUDE_SCIENCE_EXAMPLES"

#: Claude Science ships a demo project, and it is not small: on the store this
#: was built against it holds 83 of the 97 artifacts — content the user did not
#: make, outnumbering their own work six to one and, sorted by recency, well
#: placed to fill Home's ten Recent slots on its own. So it is skipped by
#: default. Matched by exact project id, because that is what distinguishes it:
#: every real project is `proj_` + a generated hex id, and the sample is the one
#: with a word instead (`proj_example`) — Claude Science's own special-casing,
#: not a heuristic of ours. A tuple rather than a lone constant so a future
#: sample can be added without reshaping the check; exact match rather than a
#: prefix so a user's own `proj_examples` is never caught by it.
EXAMPLE_PROJECT_IDS = ("proj_example",)

#: The `source` every app from this module carries, so the shell can tell a
#: read-only artifact apart from a workspace app it may scaffold and commit to.
SOURCE = "claude-science"

#: Ceiling on how many artifacts one listing will report. A researcher with a
#: few busy projects lands in the hundreds (the store this was built against
#: holds ~100); the cap exists for the pathological case, not the normal one.
MAX_ARTIFACTS = 2000

#: A version file: `v` + the digest + `_` + the name the artifact was saved as.
#: The digest length is not pinned to the 8 chars observed — matching a range
#: costs nothing and a format that grew a byte should not empty the listing.
_VERSION_RE = re.compile(r"^v[0-9a-f]{6,16}_(?P<name>.+)$", re.IGNORECASE)

# ---- the private metadata DB ------------------------------------------------
# Only ONE thing is read from it: the display name of a project, to use as the
# app's tag. Artifact names come off the filesystem (the version filename *is*
# the name the researcher saved), so a schema change can only ever cost us
# prettier tags — never the listing.
_DB_NAME = "operon-cli.db"
_DB_TIMEOUT_S = 0.5
_PROJECT_LIMIT = 1000
_ID_COLUMNS = ("id", "project_id", "slug", "uuid")
_NAME_COLUMNS = ("name", "title", "display_name", "label")


def claude_science_dir() -> str:
    """The Claude Science store: ``~/.claude-science``, or ``DIR_ENV``.

    Path only — no I/O. Normalized (expanduser + abspath) so a tilde or relative
    override yields the same absolute paths in the listing that the explorer
    would navigate to, mirroring ``shell.seed.fused_dir()``."""
    return os.path.abspath(
        os.path.expanduser(os.environ.get(DIR_ENV) or DEFAULT_DIR)
    )


def _examples_included() -> bool:
    """Whether the bundled sample project is listed (default: no).

    ``FUSED_RENDER_CLAUDE_SCIENCE_EXAMPLES`` brings it back — any set value
    except the off-words, matching how ``FUSED_RENDER_CALLS`` reads (calls.py
    ``enabled_override``), so the two switches can't mean different things by
    the same string. Read per call, not cached: this decides what a listing
    contains, and a listing is cheap to re-request."""
    raw = os.environ.get(EXAMPLES_ENV)
    if raw is None:
        return False
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _child_dirs(path: str) -> list[str]:
    """Sorted names of the non-hidden direct child DIRECTORIES of `path`.

    Hidden entries are skipped at every level, which is also what excludes the
    store's own bookkeeping — ``.thumbnails/`` and the ``.example_*_seeded``
    markers sit right beside the project dirs. Returns [] rather than raising
    when the directory is missing or unreadable: an absent store is the normal
    state on a machine without Claude Science, not an error."""
    try:
        with os.scandir(path) as it:
            return sorted(
                entry.name
                for entry in it
                if not entry.name.startswith(".") and entry.is_dir()
            )
    except OSError:
        return []


def _version_files(artifact_dir: str) -> list[os.DirEntry]:
    """The version files inside one artifact directory, newest last.

    Prefers entries matching ``v<hex>_<name>``; if none match, falls back to
    every non-hidden file in the directory. The fallback is the point: this
    module reads another application's private layout, and a renamed version
    prefix should degrade the *name* of an artifact, not make it vanish from
    the listing entirely."""
    try:
        with os.scandir(artifact_dir) as it:
            files = [e for e in it if not e.name.startswith(".") and e.is_file()]
    except OSError:
        return []
    versioned = [e for e in files if _VERSION_RE.match(e.name)]
    chosen = versioned or files

    def _key(entry: os.DirEntry):
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = 0.0
        # Name breaks mtime ties so the same directory always resolves to the
        # same entry — two versions written inside one filesystem timestamp
        # tick must not reorder the listing between requests.
        return (mtime, entry.name)

    return sorted(chosen, key=_key)


def artifact_name(filename: str) -> str:
    """The app name for a version file: ``v6f4b965a_building_h3_compare.png`` →
    ``building_h3_compare.png``.

    The version prefix comes off; **the extension stays**. Stripping it read
    better on a single card and was wrong on a real store: an analysis that
    saves a figure and its underlying table saves them under the same base name
    (`overture_coverage_matrix` as both `.png` and `.csv`), which are two
    separate artifacts — and two cards labelled identically. The extension is
    also what tells a reader which of them opens as an image and which as a
    table, and this is a file explorer: naming a file after its filename is the
    idiom here, not a leak of one.

    A file with no version prefix keeps its whole name."""
    match = _VERSION_RE.match(filename)
    return match.group("name") if match else filename


def _first_index(columns: list[str], candidates: tuple[str, ...]) -> int | None:
    for candidate in candidates:
        if candidate in columns:
            return columns.index(candidate)
    return None


def _project_names(org_dir: str) -> dict[str, str]:
    """``{project_id: display name}`` from the org's SQLite DB; {} on any doubt.

    Read-only (``mode=ro``) and time-bounded, because this database belongs to a
    running application: we take no write locks and we do not wait behind one.
    The URI is built with ``pathlib.Path.as_uri()`` — the canonical encoder for
    a file URL — so a store path containing ``?``, ``#`` or a space cannot turn
    into a different connection string than the one intended.

    Columns are introspected rather than assumed: the id and name are picked
    from ``cursor.description`` by the names they plausibly have, and if neither
    is found the caller falls back to the on-disk project id. Every sqlite error
    is swallowed to a debug line — a locked, recovering, absent or reshaped DB
    costs prettier tags and nothing else."""
    db_path = os.path.join(org_dir, _DB_NAME)
    if not os.path.isfile(db_path):
        return {}
    try:
        uri = pathlib.Path(db_path).as_uri() + "?mode=ro"
    except ValueError:
        return {}  # not an absolute path: nothing to open
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=_DB_TIMEOUT_S)
    except sqlite3.Error:
        logger.debug("claude-science: cannot open %s read-only", db_path, exc_info=True)
        return {}
    try:
        cursor = conn.execute(f"SELECT * FROM projects LIMIT {_PROJECT_LIMIT}")
        columns = [description[0].lower() for description in cursor.description]
        id_index = _first_index(columns, _ID_COLUMNS)
        name_index = _first_index(columns, _NAME_COLUMNS)
        if id_index is None or name_index is None:
            logger.debug("claude-science: no id/name column in projects (%s)", columns)
            return {}
        names = {}
        for row in cursor:
            project_id, name = row[id_index], row[name_index]
            if isinstance(project_id, str) and isinstance(name, str):
                collapsed = " ".join(name.split())
                if project_id and collapsed:
                    names[project_id] = collapsed
        return names
    except sqlite3.Error:
        logger.debug("claude-science: reading projects from %s failed", db_path,
                     exc_info=True)
        return {}
    finally:
        conn.close()


def _artifact_app(artifact_dir: str, tag: str) -> dict | None:
    """One artifact directory as an app dict, or None when it holds no version.

    An artifact with no readable file in it is skipped rather than reported
    entry-less: unlike a workspace folder — which a user made and can fill — an
    empty artifact dir is a half-written or half-deleted record in a store we
    don't own, and a card that opens onto nothing is worse than no card."""
    versions = _version_files(artifact_dir)
    if not versions:
        return None
    newest = versions[-1]
    entry = os.path.join(artifact_dir, newest.name)
    try:
        updated_at = newest.stat().st_mtime
    except OSError:
        updated_at = None
    html_entry = app_listing.is_html(entry)
    return {
        "name": artifact_name(newest.name),
        "tag": tag,
        "path": artifact_dir,
        # The file the card opens and previews — a figure or a table just as
        # readily as a page. `entry_html` stays null unless it really is one,
        # so the /render iframe (HTML-only) is never pointed at a PNG.
        "entry": entry,
        "entry_html": entry if html_entry else None,
        "title": app_listing.entry_title(entry) if html_entry else None,
        "updated_at": updated_at,
        "source": SOURCE,
    }


def _iter_apps(orgs_dir: str, include_examples: bool):
    """Every artifact in the store, lazily.

    A generator so `MAX_ARTIFACTS` can cap the WORK and not merely the output.
    The first version capped only the output: it kept iterating — and kept
    calling `_child_dirs` on every remaining project — long after it had stopped
    keeping anything, so a large store paid for a full walk on every Home
    render to produce a list it had already finished. Yielding lets the caller
    stop the walk mid-directory.
    """
    for org in _child_dirs(orgs_dir):
        org_dir = os.path.join(orgs_dir, org)
        artifacts_dir = os.path.join(org_dir, "artifacts")
        projects = _child_dirs(artifacts_dir)
        if not projects:
            continue
        # One DB read per org, not per project — and skipped entirely for an org
        # with nothing in it. Inside the generator, so an org past the cap is
        # never opened at all.
        names = _project_names(org_dir)
        for project in projects:
            if project in EXAMPLE_PROJECT_IDS and not include_examples:
                logger.debug("claude-science: skipping the sample project %r "
                             "(set %s=1 to list it)", project, EXAMPLES_ENV)
                continue
            project_dir = os.path.join(artifacts_dir, project)
            tag = names.get(project) or project
            for artifact in _child_dirs(project_dir):
                app = _artifact_app(os.path.join(project_dir, artifact), tag)
                if app is not None:
                    yield app


def list_apps() -> list[dict]:
    """Every Claude Science artifact in the store, as app dicts.

    Empty when Claude Science isn't installed (no store, or no ``orgs/``), which
    is the common case and not a condition worth reporting. Unsorted — the
    caller merges these with the workspace apps and sorts the whole list once.

    Capped at `MAX_ARTIFACTS`, and the cap STOPS THE WALK — `islice` abandons
    the generator, so nothing past the limit is listed, stat'd or opened. One
    extra `next()` is what detects that there was more; it costs one artifact's
    work and is the difference between an honest warning and a silent cap."""
    stream = _iter_apps(os.path.join(claude_science_dir(), "orgs"),
                        _examples_included())
    apps = list(itertools.islice(stream, MAX_ARTIFACTS))
    if next(stream, None) is not None:
        # Never a silent cap: a listing that dropped work says so, or the
        # missing cards read as "Claude Science has nothing else".
        logger.warning(
            "claude-science: listing capped at %d artifacts; the store holds "
            "more and the walk stopped there (store: %s)",
            MAX_ARTIFACTS, claude_science_dir())
    return apps
