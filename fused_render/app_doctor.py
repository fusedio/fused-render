"""The App Doctor report, and the per-check fix task that explains and fixes
one row of it.

Sharing an app is the moment its folder stops being private: a key pasted
into a `.py` while wiring something up, a path that only resolves on the
machine it was written on, a `__pycache__` swept along, an entry page still
declaring a fused API version the runtime stopped speaking, commits sitting
only on this machine. None of that is visible from the outside, and most of
it is deterministic to check — so the app page and the explorer's entry-page
header offer one button that runs the checks and shows them as a checklist.

THE JUDGMENT IS A SKILL. `skills/fused-render-app-doctor/` owns the review a
person actually wants — is this hit a real credential or a placeholder, is
this absolute path load-bearing or prose — and its `ci/app_check.py` is the
deterministic FLOOR of that review: the secret shapes, the device-path roots,
the structural gaps, each already tested (tests/test_app_doctor.py). This
module is not a second copy of either. It LOADS that script (by path — it is
stdlib-only and lives under a skill directory, so it is not importable as
part of the package; see `tests/_app_check_module.py`, which loads it the
same way for the same reason) and groups its findings into the checklist the
modal draws, adding only the rows the CI floor deliberately leaves out
because they need the runtime's own knowledge:

* the ENTRY rule (a page carrying `<meta name="fused-app">`, D301 — the CI
  script asks for `index.html` because a repo checkout has no runtime to ask,
  and filenames are all it has),
* the declared fused API version against the one the runtime speaks
  (`fused_api_version`),
* generated state loose in the tree instead of under `.fused/`,
* `pyproject.toml` and `icon.svg` / `icon.png` parsing, when either is there at all,
* whether the folder's own git repo has everything committed,
* whether the current branch has commits its upstream does not, and
* — the one MODEL-BACKED row — whether the views render alike across
  browsers (`cross-browser`, `app_doctor_ai.py`): on demand, cached on the
  view files' checksum, read here from that cache only.

FACT VS CANDIDATE — MEASURED, NOT ASSUMED. A run of `ci/app_check.py` over
the 8 apps in a real `~/Fused/local` produced 40 content findings, and every
one was a false positive (26 repeats of one absolute path inside committed
`runs/*.json` logs, 6 `/tmp/xxx` inside a vendored stdlib docstring, 5 paths
inside markdown code spans, 2 a deliberate constant and a relative URL, 1 a
test fixture's fake password). The two families that produce those findings
— `secrets` and `device-paths` — are pattern matches over arbitrary text:
they locate CANDIDATES, and only a read of the surrounding file decides
whether one is real. Every other row is a FACT: a file exists or does not, a
version tag reads N or does not, a folder is clean or is not — no
false-positive rate at all. `kind` records that split so the modal can read
"3 to review" instead of "3 failed" for a candidate row, and so `ok` (below)
never turns an app red on a pattern match alone.

SECTIONS AND SEVERITIES ARE DEFINED ONCE, HERE. `_CHECK_META` is the single
table of `(section, severity, kind)` per check id — `SECTIONS`, `SEVERITIES`
and `CHECK_ORDER` are read off it, and the report carries that ordering so
the modal never hardcodes a second copy. For the two ids the floor engine
itself computes (`secrets`, `device-paths`), the values come from the
engine's own `CHECK_META` when it is loaded — one source, not two — falling
back to this table's copy only when the engine cannot be loaded at all (see
`_meta`).

EVERY FACT CHECK IS DETERMINISTIC; A CANDIDATE CHECK NAMES CANDIDATES. Never
raises. A doctor that crashes on the app it was asked to examine is worse
than no doctor, so every check that touches the filesystem or a subprocess
degrades to `skip` with the reason in `detail`.
"""
import importlib.util
import os
import re

from fused_render import app_listing, fused_api_version
from fused_render.skill_sources import skill_sources

SKILL = "fused-render-app-doctor"
SKILL_QUALIFIED = f"fused-render:{SKILL}"

# The floor engine, inside the skill that owns it.
_SCRIPT_REL = os.path.join("ci", "app_check.py")

# One check as the modal draws it: an id it keys off, the label it reads, a
# state, a one-line detail, and the lines the state came from.
PASS = "pass"
FAIL = "fail"
SKIP = "skip"
# A check whose answer needs a Claude call rather than a deterministic
# read — "not run yet, press to run" — contributes nothing to `ok` and
# nothing to the severity dot until it has actually been run: an unreviewed
# question is not a failure, and it is not a pass either. One check reports
# it: `cross-browser` (`app_doctor_ai.py`), whose verdict is cached on a
# checksum of the app's view files and read back here — a GET never spawns
# the model, so a row that has never been run, or whose app changed since it
# was, is UNRUN until someone presses Check.
UNRUN = "unrun"

# ------------------------------------------------------- section/severity/kind

# The one table: every check id's `(section, severity, kind)`. Order here IS
# the order the modal draws rows in — essentials before sharing, and within
# a section the order below. See the module docstring for why `secrets` and
# `device-paths` still have an entry here despite being sourced from the
# engine when it loads: this is the fallback for when it does not.
_CHECK_META: dict[str, tuple[str, str, str]] = {
    "secrets": ("essentials", "critical", "candidate"),
    "entry": ("essentials", "critical", "fact"),
    "api-version": ("essentials", "critical", "fact"),
    "pyproject": ("essentials", "warning", "fact"),
    "readme": ("essentials", "warning", "fact"),
    "icon": ("essentials", "warning", "fact"),
    "device-paths": ("sharing", "warning", "candidate"),
    # The one MODEL-BACKED row (app_doctor_ai.py): on demand, cached on the
    # app's content, never run by a GET. A candidate, not a fact — a model's
    # reading of a rubric earns a second look before an edit, which is what
    # the candidate prompt asks the fix session for.
    "cross-browser": ("sharing", "warning", "candidate"),
    # Consolidated repo-health row (SPEC-doctor-git-ai-errors.md): committed +
    # pushed + not-behind-origin, in one. The first two are answered from
    # local refs only; "behind origin" needs a real fetch, which this never
    # performs inline — see `_repo_health_check`.
    "git": ("sharing", "warning", "fact"),
    "generated": ("sharing", "warning", "fact"),
    "preview": ("sharing", "warning", "fact"),
}

# The order every checklist is drawn in — sections in this order, and within
# essentials/sharing, the order `_CHECK_META` lists them (a plain dict
# preserves insertion order).
CHECK_ORDER = tuple(_CHECK_META.keys())
SECTIONS = ("essentials", "sharing")
# Worst first — the header button's "worst severity found" and the modal's
# chip colouring both rank against this order.
SEVERITIES = ("critical", "warning")


def _meta(cid: str) -> tuple[str, str, str]:
    """`(section, severity, kind)` for check `cid`. For `secrets` and
    `device-paths`, prefers the floor engine's own `CHECK_META` — the single
    copy of that classification — and falls back to `_CHECK_META` only when
    the engine did not load (still needed then: a skip row has to sort and
    chip the same as every other row)."""
    eng = engine()
    if eng is not None:
        table = getattr(eng, "CHECK_META", None)
        if table and cid in table:
            return table[cid]
    return _CHECK_META[cid]


# The rows a person runs by pressing the row's own Check button rather than
# the doctor answering on every GET (`POST /api/apps/doctor/run`). The modal
# reads `ondemand` off the row to draw that button — and a Re-check on a
# settled one — rather than keeping a second list of ids.
ON_DEMAND = frozenset({"cross-browser"})


def _check(cid: str, label: str, state: str, detail: str,
           findings: list | None = None) -> dict:
    section, severity, kind = _meta(cid)
    return {
        "id": cid,
        "section": section,
        "severity": severity,
        "kind": kind,
        "label": label,
        "state": state,
        "detail": detail,
        "findings": findings or [],
        "ondemand": cid in ON_DEMAND,
    }


_engine = None
_engine_error: str | None = None


def engine():
    """The loaded `app_check` module, or None when the skill is not resolvable
    (a checkout or wheel with no skills at all). Loaded once — the script is
    stdlib-only and has no state, so a second exec would buy nothing."""
    global _engine, _engine_error
    if _engine is not None or _engine_error is not None:
        return _engine
    src = skill_sources().get(SKILL)
    if not src:
        _engine_error = f"the {SKILL} skill is not installed"
        return None
    path = os.path.join(src, _SCRIPT_REL)
    if not os.path.isfile(path):
        _engine_error = f"{_SCRIPT_REL} is missing from the {SKILL} skill"
        return None
    try:
        spec = importlib.util.spec_from_file_location("fused_render_app_check", path)
        if spec is None or spec.loader is None:
            raise ImportError("no loader for the script")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — an unloadable floor is "skip", never a 500
        _engine_error = f"could not load {_SCRIPT_REL}: {exc}"
        return None
    _engine = module
    return _engine


def engine_error() -> str | None:
    """Why `engine()` came back None, for the `skip` rows' detail."""
    engine()
    return _engine_error


# ------------------------------------------------------------ generated state

# An app's own machine-local state belongs under `.fused/` (D548). These are
# the shapes that mean "generated" wherever else they turn up: caches by
# directory name, artifacts by suffix.
_GENERATED_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
_GENERATED_SUFFIXES = (".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3")

# Never walked as app content: `.fused/` is exactly where this state is
# SUPPOSED to live, `.git`/`.venv`/`node_modules` are bookkeeping whose
# contents are nobody's finding.
_SKIP_DIRS = frozenset({".fused", ".git", ".venv", "node_modules"})

# A bounded walk, for the same reason the floor engine bounds its own: a
# review of a huge half-abandoned folder must report rather than hang.
_MAX_ENTRIES = 20_000


def _generated_paths(app_dir: str) -> list[str]:
    """App-relative paths that look generated and sit outside `.fused/`,
    a cache directory reported as itself rather than by its contents."""
    out: list[str] = []
    seen = 0

    def recurse(dir_abs: str, dir_rel: str) -> bool:
        nonlocal seen
        try:
            entries = sorted(os.scandir(dir_abs), key=lambda e: e.name)
        except OSError:
            return True
        for entry in entries:
            seen += 1
            if seen > _MAX_ENTRIES:
                return False
            rel = f"{dir_rel}/{entry.name}" if dir_rel else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if entry.name in _GENERATED_DIRS:
                    out.append(rel + "/")
                    continue
                if entry.name in _SKIP_DIRS:
                    continue
                if not recurse(entry.path, rel):
                    return False
            elif entry.name.endswith(_GENERATED_SUFFIXES):
                out.append(rel)
        return True

    recurse(app_dir, "")
    return out


# ------------------------------------------------------------------ git state


def _git_pending(app_dir: str) -> tuple[str, list[tuple[str, str]]]:
    """`(state, records)` for the folder's version control: FAIL with the
    uncommitted `(code, rest)` porcelain records (app-relative, prefix
    already stripped — see `_git_findings` for what each shape means), PASS
    when the folder is clean, SKIP when git cannot answer (not a repo, git
    missing, a repo that will not read).

    Status is scoped to the app folder itself — sibling apps share one `local`
    repo (D626), and a neighbour's work in progress is not this app's finding.
    Read-only, so unlike `app_git`'s writes there is no need to establish that
    the repo is one we own: asking git about a user's own repository is the
    same question their own `git status` answers.
    """
    from fused_render import app_git

    try:
        r = app_git._git(app_dir, "status", "--porcelain", "--", ".")
    except Exception:  # noqa: BLE001 — includes a hung git past _GIT_TIMEOUT
        return SKIP, []
    if r.returncode != 0:
        return SKIP, []
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return PASS, []
    # `git status` reports paths relative to the REPO root. For an app living
    # in the shared `local` repo (D626) that root is one level up from the
    # folder being reviewed, so a prefix strip is needed to read app-relative,
    # the same as every other finding's path. But an app that IS its own repo
    # root (`_repo_scope`'s other supported layout — an unmigrated app with
    # its own `.git`) has paths already relative to app_dir with nothing to
    # strip: stripping there would wrongly collapse a same-named untracked
    # SUBdirectory (`?? demo/` inside app `demo/`) down to the empty string,
    # over-claiming the whole app as untracked when only that subdirectory
    # is (review finding: string-compare alone can't tell the two apart).
    scope = app_git._repo_scope(app_dir)
    is_own_repo_root = scope is not None and scope[1] == "."
    prefix = "" if is_own_repo_root else os.path.basename(app_dir.rstrip("/\\")) + "/"
    out = []
    for ln in lines:
        code, _, rest = ln[:2], ln[2:3], ln[3:]
        rest = rest.strip().strip('"')
        code = code.strip() or "??"
        if prefix and rest.startswith(prefix):
            rest = rest[len(prefix):]
        # `(code, rest)` — NOT a formatted string. `rest` strips to "" when
        # the WHOLE app folder is untracked (git collapses that to one `??
        # myapp/` line, and `rest == prefix` exactly), and stays non-empty
        # but directory-shaped (`"sub/"`) for a nested untracked directory
        # git also collapses. `_git_findings` below turns each shape into a
        # readable finding — this function only parses porcelain output, it
        # does not decide how a shape reads.
        out.append((code, rest))
    return FAIL, out


def _git_findings(pending: list[tuple[str, str]]) -> list[dict]:
    """One finding per `(code, rest)` pair from `_git_pending`, in the shape
    `_git_check` hands to the modal and the fix prompt.

    Three shapes, by what `rest` looks like after `_git_pending`'s prefix
    strip:

    * EMPTY — the whole app folder is untracked (git's `?? myapp/` collapse,
      stripped down to nothing). `path` is `.` (there is no more specific
      path to give) and the excerpt says so in words, ending in a period so
      the modal's `excerpt.includes(path)` dedup guard still fires on the
      literal `.` — the same trick every other shape below relies on, just
      earned differently.
    * A DIRECTORY — `rest` still ends in `/` (git's `?? sub/` collapse for a
      nested untracked directory: nothing inside was walked, so there is
      nothing more specific to report). Reads as the directory itself, not
      the bare porcelain line.
    * ORDINARY — a real file entry (`M app.py`, `?? notes.txt`). Unchanged
      from before this fix: `path` and `excerpt` are both `"{code} {rest}"`,
      which is what existing tests pin and what the modal's dedup guard
      already handles.
    """
    out = []
    for code, rest in pending:
        if not rest:
            out.append({"rule": "git:uncommitted", "path": ".", "line": 0,
                       "excerpt": "the whole app folder is untracked."})
        elif rest.endswith("/"):
            out.append({"rule": "git:uncommitted", "path": rest, "line": 0,
                       "excerpt": f"{rest} (untracked directory)"})
        else:
            p = f"{code} {rest}"
            out.append({"rule": "git:uncommitted", "path": p, "line": 0, "excerpt": p})
    return out


# `_pushed_pending`'s SKIP reasons — distinct enough that `_repo_health_check`
# can say something TRUE instead of one catch-all sentence. `NO_REPO` covers
# "not a git repository this server can read" in all its shapes (no `.git`
# at all, `git` missing from PATH, a hung call past `_GIT_TIMEOUT`) — the
# same condition `_git_pending` reports too, worded the same way. `NO_UPSTREAM`
# is the one case that is actually about THIS question: a real, readable
# repo whose current branch has no upstream
# configured, so there is nothing to compare against.
_SKIP_NO_REPO = "no-repo"
_SKIP_NO_UPSTREAM = "no-upstream"


def _pushed_pending(app_dir: str) -> tuple[str, list[str], str]:
    """`(state, subjects, skip_reason)` for whether the current branch is
    ahead of its upstream: FAIL with the unpushed commits' subject lines,
    PASS when there is nothing to push, SKIP — with `skip_reason` telling
    apart WHY — when there is no upstream configured, no remote, this folder
    is not a git repository this server can read, or git cannot answer at
    all. `skip_reason` is `""` except when `state` is SKIP.

    NO NETWORK CALL. `@{upstream}..HEAD` is answered entirely from the local
    refs git already has — never `git fetch`, never `git ls-remote` — so this
    can be as stale as the last fetch. That is the right tradeoff for a
    button press: a doctor report is read-only and cheap, the same
    read-only, degrade-to-skip discipline `_git_pending` follows above, and
    for the same reason — scoped with `-C app_dir` so a subdirectory of the
    shared `local` repo (D626) still resolves through git's own upward
    search, exactly like `_git_pending`.

    SCOPED WITH `-- .`, exactly like `_git_pending` (D626): sibling apps share
    one `local` repo, so an unpathspec'd `rev-list`/`log` counts and quotes
    EVERY app's unpushed commits, not just this one's — a neighbour's commit
    subjects handed to this app's fix session would be actively misleading,
    not just noisy.

    A cheap `rev-parse --is-inside-work-tree` decides `NO_REPO` vs
    `NO_UPSTREAM` up front rather than sniffing `rev-list`'s stderr for a
    particular sentence — git's error text is not a stable API across
    versions or locales, a boolean answer from a purpose-built flag is."""
    from fused_render import app_git

    try:
        probe = app_git._git(app_dir, "rev-parse", "--is-inside-work-tree")
    except Exception:  # noqa: BLE001 — includes a hung git past _GIT_TIMEOUT
        return SKIP, [], _SKIP_NO_REPO
    if probe.returncode != 0 or (probe.stdout or "").strip() != "true":
        return SKIP, [], _SKIP_NO_REPO

    try:
        r = app_git._git(app_dir, "rev-list", "--count", "@{upstream}..HEAD", "--", ".")
    except Exception:  # noqa: BLE001
        return SKIP, [], _SKIP_NO_REPO
    if r.returncode != 0:
        # A readable repo, but this branch has no upstream tracking ref (or
        # no remote at all) — the one SKIP cause that is actually about
        # "nothing to compare against" rather than "can't read git here".
        return SKIP, [], _SKIP_NO_UPSTREAM
    try:
        count = int((r.stdout or "").strip() or "0")
    except ValueError:
        return SKIP, [], _SKIP_NO_UPSTREAM
    if count <= 0:
        return PASS, [], ""
    try:
        r2 = app_git._git(app_dir, "log", "--format=%s", "@{upstream}..HEAD", "--", ".")
    except Exception:  # noqa: BLE001
        return SKIP, [], _SKIP_NO_REPO
    if r2.returncode != 0:
        return SKIP, [], _SKIP_NO_UPSTREAM
    subjects = [ln for ln in (r2.stdout or "").splitlines() if ln.strip()]
    return FAIL, subjects, ""


# --------------------------------------------------------------- file parsing


def _parses(path: str, kind: str) -> tuple[bool, str]:
    """`(ok, reason)` for a file whose whole check is "does it parse". Absence
    is the caller's business — both files this is used for are optional."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return False, exc.strerror or str(exc)
    try:
        if kind == "toml":
            import tomllib

            tomllib.loads(raw.decode("utf-8"))
        elif kind == "png":
            # The 8-byte PNG signature — a renamed jpeg or an empty file is the
            # finding; decoding the whole image is not this check's business.
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("not a PNG file (bad signature)")
        else:
            from xml.etree import ElementTree

            ElementTree.fromstring(raw)
    except Exception as exc:  # noqa: BLE001 — any parse failure is the finding
        return False, str(exc).splitlines()[0] if str(exc) else "does not parse"
    return True, ""


# --------------------------------------------------------------------- report

_FAMILY_CHECKS = (
    ("secrets", "No leaked credentials", "secrets:",
     "nothing shaped like a live credential"),
    ("device-paths", "No paths tied to one machine", "device-path:",
     "no absolute path that only resolves on one machine"),
)


def _entry_check(entry: str | None) -> dict:
    return _check(
        "entry", "Has an app entry page", PASS if entry else FAIL,
        f"{os.path.basename(entry)} carries the fused-app marker" if entry
        else 'no page in this folder carries <meta name="fused-app"> — '
             "without one there is nothing for whoever you share it with to open",
    )


def _api_version_check(entry: str | None) -> dict:
    # Only askable of an entry, and only meaningful when the runtime knows
    # its own version (no migration docs = version 0, and nothing is behind 0).
    current = fused_api_version.current_version()
    if entry is None:
        return _check("api-version", "Declares the current fused API version",
                      SKIP, "no entry page to read the version tag off")
    if current <= 0:
        return _check("api-version", "Declares the current fused API version",
                      SKIP, "this runtime does not report a fused API version")
    declared = fused_api_version.api_version(entry)
    behind = declared < current
    return _check(
        "api-version", "Declares the current fused API version",
        FAIL if behind else PASS,
        f"declares version {declared}; the runtime is on {current} — "
        f"the `fused` calls in this app may have moved since"
        if behind else f"version {current}, the one the runtime speaks",
    )


def _content_family_checks(app_dir: str) -> dict[str, dict]:
    """`{"secrets": check, "device-paths": check}` — the floor engine's two
    pattern-match families, grouped into rows. A skip on one is a skip on
    both: they come off the same `check()` call, so either both ran or
    neither did."""
    eng = engine()
    out: dict[str, dict] = {}
    if eng is None:
        why = engine_error() or "the check engine is unavailable"
        for cid, label, _prefix, _clean in _FAMILY_CHECKS:
            out[cid] = _check(cid, label, SKIP, why)
        return out
    why = ""
    try:
        findings = eng.check(app_dir)
    except Exception as exc:  # noqa: BLE001 — `check` promises not to raise; trust nothing
        findings = None
        why = f"the check engine failed: {exc}"
    for cid, label, prefix, clean in _FAMILY_CHECKS:
        if findings is None:
            out[cid] = _check(cid, label, SKIP, why)
            continue
        hits = [f for f in findings if str(f.get("rule", "")).startswith(prefix)]
        hits.sort(key=lambda f: (f.get("path", ""), f.get("line", 0), f.get("rule", "")))
        out[cid] = _check(
            cid, label, FAIL if hits else PASS,
            f"{len(hits)} line{'' if len(hits) == 1 else 's'} to look at"
            if hits else clean,
            hits,
        )
    return out


def _generated_check(app_dir: str) -> dict:
    stray = _generated_paths(app_dir)
    return _check(
        "generated", "No generated files outside .fused/",
        FAIL if stray else PASS,
        f"{len(stray)} generated path{'' if len(stray) == 1 else 's'} in the app "
        "tree — delete them, move them under .fused/, or gitignore them"
        if stray else "no caches or build artifacts loose in the folder",
        [{"rule": "generated:stray", "path": p, "line": 0, "excerpt": p} for p in stray],
    )


def _readme_check(app_dir: str) -> dict:
    try:
        names = os.listdir(app_dir)
    except OSError:
        names = []
    has_readme = any(n.lower().startswith("readme")
                     and os.path.isfile(os.path.join(app_dir, n)) for n in names)
    return _check(
        "readme", "Has a README explaining the app", PASS if has_readme else FAIL,
        "a README says what this is" if has_readme
        else "no README — say what this app does for whoever you share it with",
    )


def _preview_check(app_dir: str) -> dict:
    preview = os.path.join(app_dir, app_listing.PREVIEW_IMAGE_NAME)
    try:
        has_preview = os.path.isfile(preview) and os.path.getsize(preview) > 0
    except OSError:
        has_preview = False
    return _check(
        "preview", "Has a preview.png thumbnail", PASS if has_preview else FAIL,
        "preview.png is what a card shows" if has_preview
        else f"no {app_listing.PREVIEW_IMAGE_NAME} (or it is empty) — this is how "
             "the app is recognized in a grid of others",
    )


def _optional_file_check(app_dir: str, cid: str, name: str, kind: str, label: str) -> dict:
    path = os.path.join(app_dir, name)
    if not os.path.isfile(path):
        return _check(cid, label, SKIP, f"no {name} in this folder")
    ok, reason = _parses(path, kind)
    return _check(cid, label, PASS if ok else FAIL,
                 f"{name} parses" if ok else f"{name}: {reason}")


def _pyproject_check(app_dir: str) -> dict:
    return _optional_file_check(app_dir, "pyproject", "pyproject.toml", "toml",
                                "pyproject.toml is valid TOML")


def _icon_check(app_dir: str) -> dict:
    # The same precedence the shell draws by (app_listing.ICON_NAMES): the svg
    # is the icon when both are there, so it is the one that has to parse.
    label = "icon.svg / icon.png is a valid image"
    for name in app_listing.ICON_NAMES:
        if os.path.isfile(os.path.join(app_dir, name)):
            kind = "png" if name.endswith(".png") else "xml"
            return _optional_file_check(app_dir, "icon", name, kind, label)
    return _check("icon", label, SKIP, "no icon.svg or icon.png in this folder")


def _repo_health_check(app_dir: str) -> dict:
    """The consolidated Sharing-section row: every change committed, every
    commit pushed, and not behind origin — in one (SPEC-doctor-git-ai-
    errors.md). The first two are answered entirely from local refs
    (`_git_pending`/`_pushed_pending`, no network). "Behind origin" needs a
    real fetch, which THIS FUNCTION NEVER PERFORMS ITSELF: it only calls
    `git_upstream.note_app_opened`, which does no synchronous git work of its
    own — it dispatches a throttled, best-effort BACKGROUND check (see that
    module's docstring) and returns immediately. What follows reads whatever
    `git_upstream`'s own cache (`repo_state_for`) already has for this repo
    root: fresh from a moment ago, stale from up to `CHECK_TTL_S` earlier, or
    still empty because the fetch has not resolved (or ever will — offline,
    no remote, expired auth). A slow or unreachable remote can therefore
    never make a doctor report slow; a caller that reopens Doctor a moment
    later sees the row filled in once the background fetch has landed.

    A remote that is unresolved reads as SKIP, with the reason stated — never
    FAIL: an unreachable remote is not a defect in the app being reviewed.

    MIXED REF BASES, NAMED HONESTLY RATHER THAN UNIFIED: `p_state`/`p_subjects`
    (the "unpushed" half, from `_pushed_pending`) compare against `@{upstream}`
    — this branch's OWN tracking ref, whole-repo, matching the in-app Git
    panel's "Send N" count exactly (`templates/git/log.py`'s `rev-list
    --left-right --count HEAD...@{upstream}`). `behind`/`ahead` (the "not
    behind origin" half, from `git_upstream.check_repo`) instead compare
    against `origin/<default_branch>` — deliberately: the Pull button this
    row can offer always fast-forwards onto the DEFAULT branch
    (`git_upstream.update_repo`), never onto `@{upstream}`, so that is the
    only base that tells the truth about what Pull would do. On the default
    branch the two bases usually agree (its own upstream typically IS
    `origin/<default_branch>`), so this row says "origin". Off the default
    branch they answer different questions on purpose — "is MY branch pushed
    anywhere" vs. "is the DEFAULT branch's tip ahead of me" — and saying
    "behind origin" there would misname which ref that count is actually
    against; `_behind_target` below names the real one instead of pretending
    the two bases are one.
    """
    from fused_render import git_upstream

    g_state, g_pending = _git_pending(app_dir)
    p_state, p_subjects, p_skip_reason = _pushed_pending(app_dir)

    root = git_upstream.repo_root(app_dir)
    behind = ahead = on_default = clean = cached = None
    default_branch = None
    if root is not None:
        git_upstream.note_app_opened(app_dir)
        cached = git_upstream.repo_state_for(root)
        if cached is not None:
            behind = cached.get("behind") or 0
            ahead = cached.get("ahead") or 0
            on_default = cached.get("on_default")
            default_branch = cached.get("default_branch")
        # The SAME cleanliness signal `update_repo`'s preflight refuses on
        # (`_mutation_preflight` -> `_is_clean(root, include_untracked=False)`)
        # — computed here, synchronously, local-only (no fetch, no network:
        # just `git status --porcelain`), so the UI can know BEFORE the user
        # presses Pull whether it would be refused with "dirty". This is
        # deliberately the whole-repo signal `update_repo` actually checks,
        # not `g_state` (this app's own `-- .`-scoped view) — a Pull button
        # this row shows acts on the whole repo root, and a sibling app's
        # uncommitted change also blocks a real `--ff-only` pull here even
        # though it never fails g_state.
        try:
            clean = git_upstream._is_clean(root, include_untracked=False)
        except Exception:  # noqa: BLE001 — unknown status reads as not-clean
            clean = False

    findings = _git_findings(g_pending) + [
        {"rule": "pushed:unpushed", "path": ".", "line": 0, "excerpt": s}
        for s in p_subjects
    ]

    # Pull is only ever a real option when the fetch confirmed a positive
    # behind count AND the row's own on_default/clean signals say
    # `update_repo`'s preflight (not-default / dirty) would not refuse it —
    # see B1 in FIXES-round-1.md. `showsPullAction` (appdoctor-lib.ts) makes
    # the identical decision from these same three fields; keep the two in
    # sync rather than re-deriving one from the other.
    can_pull = bool(behind) and on_default is True and clean is True

    # What `behind`/`ahead` are actually measured against — `origin` on the
    # default branch (where that coincides with `@{upstream}` for almost
    # every repo), or the default branch's own name off it, where "behind
    # origin" would misleadingly suggest this is the same comparison
    # `p_state` just made against this branch's own upstream (see this
    # function's docstring).
    behind_target = "origin" if on_default else (default_branch or "the default branch")

    failing_bits = []
    if g_state == FAIL:
        failing_bits.append(
            f"{len(g_pending)} uncommitted path{'' if len(g_pending) == 1 else 's'}")
    if p_state == FAIL:
        failing_bits.append(
            f"{len(p_subjects)} unpushed commit{'' if len(p_subjects) == 1 else 's'}")
    if behind:
        failing_bits.append(
            f"{behind} commit{'' if behind == 1 else 's'} behind {behind_target}")

    if failing_bits:
        state = FAIL
        detail = ", ".join(failing_bits) + " — " + _repo_health_advice(
            commit=g_state == FAIL, push=p_state == FAIL, behind=behind,
            can_pull=can_pull, on_default=on_default, clean=clean,
            default_branch=(cached.get("default_branch") if cached is not None
                             else None),
        )
    elif p_skip_reason == _SKIP_NO_UPSTREAM:
        # `behind`/`ahead` may still be a confirmed number here (git_upstream
        # compares HEAD against the DEFAULT branch's origin ref regardless of
        # this branch's own upstream — see api.ts's `ahead` doc comment) but
        # that is not the same question as "is every commit on THIS branch
        # pushed somewhere" — with no upstream configured, that half of the
        # row can never be confirmed, so this must not read as PASS (the
        # previous `pushed` row correctly SKIPped here; the consolidation
        # regressed it — B2 in FIXES-round-1.md).
        state = SKIP
        detail = "no upstream remote configured for this folder — nothing to compare against"
    elif behind is not None:
        state = PASS
        detail = f"the working tree is clean, nothing to push, and up to date with {behind_target}"
    else:
        state = SKIP
        if root is None:
            detail = "this folder is not in a git repository this server can read"
        else:
            detail = "origin status could not be checked (offline, unreachable, or not checked yet)"

    row = _check("git", "Repo in sync", state, detail, findings)
    # Additive, UI-only fields — not part of the fixed `_check` shape every
    # other row returns, since only this row has a remote to name or a Pull
    # action to offer. `gitRoot` (camelCase — read by the TS client, api.ts's
    # `AppCheck`) is the path `POST /api/git-upstream` takes as `root`.
    row["behind"] = behind
    row["ahead"] = ahead
    row["gitRoot"] = root
    # `onDefault`/`clean` (camelCase, api.ts's `AppCheck`) — B1: the same two
    # signals `RepoUpdatesDock` already gates its own Update button on, so
    # `showsPullAction` can refuse the same way `update_repo`'s preflight
    # would, instead of offering a button that is a guaranteed dead end.
    row["onDefault"] = on_default
    row["clean"] = clean
    return row


def _repo_health_advice(*, commit, push, behind, can_pull, on_default, clean,
                         default_branch):
    """The `git` row's trailing advice sentence — F2 in FIXES-round-1.md: it
    must name only the actions that would actually help, in `commit, push, or
    pull` order, never a generic "pull, commit, or push" that mentions an
    action nothing failed over.

    When origin has moved on (`behind`) but the row's own on_default/clean
    signals mean a direct Pull would be refused (B1), this says what has to
    change FIRST — the same fact `showsPullAction` uses to hide the button —
    rather than telling the user to "pull" into a button that is not shown.

    `clean` is the WHOLE-repo dirty signal (Pull's own preflight), while
    `commit` is this row's own PATH-scoped one — a dirty path always implies
    a dirty whole repo, so when both fire the blocker must not re-mention
    "commit" a second time (G2 in FIXES-round-3.md: this used to read
    "commit or commit or stash your changes to pull...")."""
    bits = []
    if commit:
        bits.append("commit")
    if push:
        bits.append("push")
    if behind:
        if can_pull:
            bits.append("pull")
        else:
            blockers = []
            if on_default is not True:
                blockers.append(f"switch to {default_branch or 'the default branch'}")
            if clean is not True:
                blockers.append("stash your changes" if commit else "commit or stash your changes")
            bits.append(" and ".join(blockers) + " to pull" if blockers else "pull")
    if not bits:
        return "pull, commit, or push so what you share matches what you tested"
    if len(bits) == 1:
        joined = bits[0]
    elif len(bits) == 2:
        joined = f"{bits[0]} or {bits[1]}"
    else:
        joined = ", ".join(bits[:-1]) + f", or {bits[-1]}"
    return joined + " so what you share matches what you tested"


def _cross_browser_check(app_dir: str) -> dict:
    """The model-backed row, FROM ITS CACHE ONLY — `app_doctor_ai.row_state`
    never spawns anything. The run itself is the router's
    `POST /api/apps/doctor/run`, which writes the cache this reads."""
    from fused_render import app_doctor_ai

    try:
        state, detail, findings = app_doctor_ai.row_state(app_dir)
    except Exception as exc:  # noqa: BLE001 — a doctor never crashes on its patient
        state, detail, findings = UNRUN, f"could not read the last verdict: {exc}", []
    return _check(app_doctor_ai.CHECK_ID, app_doctor_ai.LABEL, state, detail, findings)


# One computation per check id, dispatched by `report` (which needs every row)
# and `report_one` (which needs exactly one, without paying for the rest).
_ENTRY_DEPENDENT = ("entry", "api-version")


def _compute_entry(app_dir: str) -> str | None:
    try:
        return app_listing.app_entry(app_dir)
    except OSError:
        return None


def report(app_dir: str) -> dict:
    """The whole checklist for one app folder.

    `{"path", "entry", "ok", "checks": [...], "sections", "severities"}`.
    `checks` is always in `CHECK_ORDER` — essentials, then sharing, in the
    order `_CHECK_META` lists them — so the modal can group by reading
    `section` off consecutive rows rather than sorting them itself.

    `ok` is "no FAILING check of severity critical or warning" — since those
    are the only two severities a checklist row carries (see `SEVERITIES`),
    this is "no failing row at all". A candidate row (`secrets`,
    `device-paths`) still counts at its own severity: `ok` does not know or
    care whether a row is a fact or a candidate, only whether it failed and
    how serious that would be if real. The modal is what tells a failing
    candidate apart from a settled failure (see appdoctor-lib.ts)."""
    app_dir = os.path.abspath(app_dir)
    entry = _compute_entry(app_dir)

    by_id: dict[str, dict] = {
        "entry": _entry_check(entry),
        "api-version": _api_version_check(entry),
        **_content_family_checks(app_dir),
        "generated": _generated_check(app_dir),
        "readme": _readme_check(app_dir),
        "preview": _preview_check(app_dir),
        "pyproject": _pyproject_check(app_dir),
        "icon": _icon_check(app_dir),
        "git": _repo_health_check(app_dir),
        "cross-browser": _cross_browser_check(app_dir),
    }
    checks = [by_id[cid] for cid in CHECK_ORDER]

    return {
        "path": app_dir,
        "entry": entry,
        "ok": not any(c["state"] == FAIL and c["severity"] in ("critical", "warning")
                     for c in checks),
        "checks": checks,
        "sections": list(SECTIONS),
        "severities": list(SEVERITIES),
    }


def report_one(app_dir: str, check_id: str) -> dict | None:
    """Just ONE row of `report`, computed without the rest of the walk — a
    row refreshing itself after its own fix task lands, rather than the
    modal re-running every check to update one. None for an unknown id.

    Cheaper than it looks for most ids (a `readme` re-check is one
    `os.listdir`), and no cheaper than the full report for the two ids that
    share one engine call (`secrets`, `device-paths`) — there is no way to
    ask the floor engine for only one of its two families."""
    if check_id not in _CHECK_META:
        return None
    app_dir = os.path.abspath(app_dir)

    if check_id in _ENTRY_DEPENDENT:
        entry = _compute_entry(app_dir)
        return _entry_check(entry) if check_id == "entry" else _api_version_check(entry)
    if check_id in ("secrets", "device-paths"):
        return _content_family_checks(app_dir)[check_id]
    if check_id == "generated":
        return _generated_check(app_dir)
    if check_id == "readme":
        return _readme_check(app_dir)
    if check_id == "preview":
        return _preview_check(app_dir)
    if check_id == "pyproject":
        return _pyproject_check(app_dir)
    if check_id == "icon":
        return _icon_check(app_dir)
    if check_id == "git":
        return _repo_health_check(app_dir)
    if check_id == "cross-browser":
        return _cross_browser_check(app_dir)
    raise AssertionError(f"unreachable: {check_id!r} is in _CHECK_META but not dispatched")


# ------------------------------------------------------------------- the task

# The first words of every App Doctor task's prompt — how a stored task is
# recognised as one (`server/routers/apps.py`) without a second field on the
# schedule entry, the same trick the migration task uses.
DOCTOR_PROMPT_PREFIX = "Run App Doctor on this fused-render app"

# The check id is embedded right after the prefix as `` — check `<id>` `` so
# a stored prompt can be read back apart without a second field on the
# schedule entry: `is_doctor_prompt` only needs the fixed words (a running
# task on ANY row still counts for the one-fix-session-per-app gate),
# `doctor_task_check_id` needs the id too (per-check live-task attachment).
_CHECK_ID_RE = re.compile(r"check `([a-z0-9-]+)`")


def is_doctor_prompt(message: str) -> bool:
    """A FIX task's prompt OR a CHECK task's (`check_prompt`) — both are App
    Doctor tasks for the one-live-task-per-app gate (a check must not read
    files a fix is rewriting, and a fix must not start on a verdict that is
    still being written). `is_doctor_check_prompt` tells them apart."""
    return str(message or "").startswith(DOCTOR_PROMPT_PREFIX)


# The words right after the prefix that mark a CHECK task (`check_prompt`)
# rather than a fix: the on-demand row's own model run, which the panel
# draws as "checking" rather than "Fix in progress".
_CHECK_TASK_MARK = " — run check `"


def is_doctor_check_prompt(message: str) -> bool:
    text = str(message or "")
    return text.startswith(DOCTOR_PROMPT_PREFIX + _CHECK_TASK_MARK)


def doctor_task_check_id(message: str) -> str | None:
    """The check id embedded in a stored App Doctor prompt, or None — either
    this is not a doctor prompt at all, or it predates the id being embedded
    (a task stored by a previous build)."""
    if not is_doctor_prompt(message):
        return None
    m = _CHECK_ID_RE.search(str(message))
    return m.group(1) if m else None


# "Fix all" (server/routers/apps.py) is not a check id — it is the whole
# report — but its task is still an App Doctor prompt, so it needs the same
# `` check `<id>` `` slot `doctor_task_check_id` reads, just with a value
# that is never a real row.
ALL = "all"


def _findings_block(detail: str, findings: list[dict]) -> str:
    """`detail` and `findings` as the lines a fix session reads. Shared by
    `doctor_prompt` and `doctor_prompt_all` so the two prompts describe a
    row identically.

    `detail` is the row's own one-line diagnosis (`_check`'s `detail`) — for
    a `kind="fact"` row (`api-version`, `readme`, `git`, ...) it is the WHOLE
    diagnosis, findings is always `[]` for those, and there used to be a
    fallback line here pointing at a `detail` the prompt never actually
    included. That fallback is gone: `detail` always goes in, first, so
    there is no longer a row with nothing to say. For a `kind="candidate"`
    row (`secrets`, `device-paths`) `detail` is a one-line count ("2 lines
    to look at") and the findings below it are the substance — both go in,
    detail first."""
    lines = []
    if detail:
        lines.append(f"- {detail}")
    lines.extend(
        "- " + (f"{f.get('path', '.')}:{f['line']}" if f.get("line") else
                str(f.get('path', '.')))
        + f": {f.get('rule', '')}: {f.get('excerpt', '')}"
        # A model-backed row (`cross-browser`) also says what to change; a
        # deterministic row's findings have no such line and add nothing.
        + (f" — suggested fix: {f['fix']}" if f.get("fix") else "")
        for f in findings
    )
    return "\n".join(lines)


def _triage_ask(kind: str) -> str:
    """What the prompt asks of the session for one row, given its `kind` —
    triage first for a candidate (the pattern that flagged it does not
    decide by itself, see the module docstring's measurement), fix outright
    for a fact."""
    if kind == "candidate":
        return (
            "This is a CANDIDATE row: the pattern that flagged each finding below does "
            "not by itself mean it is real. Triage every one first — say which are real "
            "and which are a false positive (a fixture, a vendored file, documented "
            "prose, a deliberate constant) and why — then fix only the ones you judged "
            "real."
        )
    return (
        "Fix what is safe to fix here, and say in one line what each finding is and "
        "why it matters."
    )


_CREDENTIAL_NOTE = (
    "For a leaked credential, say where it is and that it needs rotating; do not move "
    "the value somewhere else and call it fixed. Leave everything the skill does not "
    "ask about alone."
)

# R3: App Doctor itself never writes — the fix SESSION does, and until now no
# prompt told it to leave its own edits committed, so a reopened report saw
# the `git` row FAIL right after a fix landed, reading as if the fix broke
# something. Two wordings, not one, because "fix all" makes ONE commit for
# the whole run rather than one per row (`doctor_prompt_all` already says
# "one row at a time in the order given" — this is the trailing step after
# that, not a per-row addition).
#
# `_COMMIT_STEP` is deliberately conditional ("only if you changed something")
# so an advisory-only outcome — every `secrets` row that follows
# `_CREDENTIAL_NOTE` above: report the leak, don't touch the file — never
# produces an empty or spurious commit. And it never says "commit the fix",
# which for `secrets` could be misread as license to commit a value merely
# relocated rather than left alone; the wording instead says plainly that a
# still-live or relocated credential must never end up in the commit.
#
# Review findings 1 & 2, one root cause: `_COMMIT_STEP` is only right for a
# row whose fix EDITS FILES. `git` (the consolidated commit/push/behind row)
# doesn't — its fix IS a git action, already spelled out in its own SKILL.md
# section ("Commit the listed paths" / "Push the branch" / "pull"). Appending
# `_COMMIT_STEP` on top contradicts it: its "never push" fights the push
# fix, and its "no edit at all -> no commit" fights the commit fix, which
# commits paths that are already uncommitted with no edit required. So
# `doctor_prompt` skips `_COMMIT_STEP` entirely for `git` — its own SKILL.md
# section is the whole instruction, untouched.
_COMMIT_STEP = (
    "If you edited any files to address this, commit them now in this repo — never "
    "push. Say what changed in the commit message. If you made no edit at all (an "
    "advisory-only outcome, including a secrets finding where the right move was "
    "reporting it rather than touching the file), make no commit — not an empty one, "
    "and never one that commits a still-live or merely relocated credential."
)

# The `_COMMIT_STEP` per-row rows this trailing step must not be appended
# after (see the note above `_COMMIT_STEP`) — their own SKILL.md sections
# already cover committing/pushing, and a blanket "never push" or
# edit-conditioned commit instruction here would contradict them exactly the
# same way `_COMMIT_STEP` would on their own per-row prompts.
_COMMIT_STEP_EXEMPT = frozenset({"git"})

# Fix-all's trailing step covers a run that may include the `git` row
# alongside ordinary file-editing rows, so — unlike `_COMMIT_STEP` — it must
# not blanket-forbid push or restrict the commit to only files edited THIS
# run: it explicitly defers to that row's own section above when it is
# present (review findings 1 & 2 applied to the fix-all case).
_COMMIT_STEP_ALL = (
    "When every row above is done: make ONE commit — not one per row — covering every "
    "file you actually edited across the whole run, plus any pre-existing uncommitted "
    "paths a `git` row above asked you to commit even though you didn't edit them. This "
    "step itself does not include pushing — except if a `git` row is one of the rows "
    "above and it asked you to push, in which case follow through on push exactly as "
    "that row's own section says; this step does not forbid it. If nothing needed "
    "changing anywhere (every row was advisory only, or already passed) and no row "
    "above asked for a commit or push of its own, make no commit at all — and never "
    "commit a still-live or merely relocated credential from a secrets row."
)


def doctor_prompt(entry_html: str, check_id: str, findings: list[dict],
                  detail: str = "") -> str:
    """The fix task's text for ONE row: names the check, points at the
    skill's section for it by id (SKILL.md's section names match check ids
    exactly, see its own module note), and carries that row's findings
    inline so the session does not have to re-derive them by re-running the
    checks itself.

    A candidate row (`secrets`, `device-paths`) asks for triage FIRST — see
    `_triage_ask`. `kind` comes from `_meta`, not a direct `_CHECK_META`
    read — `_meta` prefers the floor engine's own `CHECK_META` for `secrets`/
    `device-paths`, the single source of truth for their classification. A
    direct `_CHECK_META` read would keep asking for a triage-first fix (or a
    fix-outright one) based on this module's fallback copy even after the
    engine reclassified an id — fix-outright where triage-first was needed
    is exactly the failure the candidate/fact split exists to prevent.

    `detail` is the row's own diagnosis (`report_one(...)["detail"]`) — R1:
    every prompt has to carry it, not just the findings list, since most
    checks (every `kind="fact"` row) never populate `findings` at all and
    `detail` is their whole story.

    Review finding 3: `router` (`apps.py`) passes `findings=[]`, `detail=""`
    whenever `report_one()` returns `None` — `_findings_block` then returns
    `""` and the "Findings for this row:" header is omitted rather than left
    dangling over nothing.

    Review findings 1 & 2: `git` is excluded from the trailing `_COMMIT_STEP`
    (see the note above that constant) — its own SKILL.md section is the
    whole instruction for that row."""
    entry_name = os.path.basename(entry_html)
    _section, _severity, kind = _meta(check_id)
    lines = _findings_block(detail, findings)
    ask = _triage_ask(kind)
    findings_section = f"Findings for this row:\n{lines}\n\n" if lines else ""
    commit_step = "" if check_id in _COMMIT_STEP_EXEMPT else _COMMIT_STEP

    return (
        f"{DOCTOR_PROMPT_PREFIX} — check `{check_id}` (`{entry_name}` is its entry page). "
        f"Invoke the `{SKILL_QUALIFIED}` skill and read its `{check_id}` section end to "
        f"end. {ask} {_CREDENTIAL_NOTE}\n\n"
        f"{findings_section}{commit_step}"
    ).rstrip()


def doctor_prompt_all(entry_html: str, checks: list[dict]) -> str:
    """"Fix all"'s text: one session covering every FAILING row in `checks`
    (as `report()` returns them — section order already), each with its own
    findings inline and its own triage-or-fix instruction. The per-row
    fix (`doctor_prompt`) and this one describe a row identically — the only
    difference is how many rows are in the prompt and that this one names no
    single check by id (`ALL` fills that slot in the stored prompt instead,
    so `is_doctor_prompt` and the one-live-task-per-app gate still work
    unchanged).

    R1: each row's own `detail` is included alongside its findings, same as
    `doctor_prompt` — every check dict already carries `detail` (`_check`),
    so no re-derivation needed here. R3: ONE trailing commit step covers the
    whole run, appended after every row's block — never one per row, and
    only when there was at least one failing row to maybe act on."""
    entry_name = os.path.basename(entry_html)
    failing = [c for c in checks if c["state"] == FAIL]
    if not failing:
        return (
            f"{DOCTOR_PROMPT_PREFIX} — check `{ALL}` (`{entry_name}` is its entry page, "
            f"covering every failing row). Invoke the `{SKILL_QUALIFIED}` skill and "
            f"follow it end to end, one row at a time in the order given. "
            f"{_CREDENTIAL_NOTE}\n\n"
            "Every check passed — say so and stop; there is nothing to fix."
        )

    blocks = []
    for c in failing:
        blocks.append(
            f"## `{c['id']}` — {c['label']}\n"
            f"{_triage_ask(c['kind'])}\n"
            f"{_findings_block(c['detail'], c['findings'])}"
        )
    body = "\n\n".join(blocks)

    return (
        f"{DOCTOR_PROMPT_PREFIX} — check `{ALL}` (`{entry_name}` is its entry page, "
        f"covering every failing row). Invoke the `{SKILL_QUALIFIED}` skill and follow "
        f"it end to end, one row at a time in the order given. {_CREDENTIAL_NOTE}\n\n"
        f"{body}\n\n{_COMMIT_STEP_ALL}"
    )


def check_prompt(entry_html: str, files: list[str], verdict_path: str) -> str:
    """The CHECK task's text for the on-demand `cross-browser` row
    (`app_doctor_ai`): a session that READS the listed view files against the
    cross-browser skill's trap table and writes ONE file — the verdict, at
    the path the server chose under `.fused/cache/` — and edits nothing else.

    Starts with `DOCTOR_PROMPT_PREFIX` so the one-live-task-per-app gate
    counts it (`is_doctor_prompt`), then `_CHECK_TASK_MARK` + the id so
    `is_doctor_check_prompt` and `doctor_task_check_id` both read it. The
    session is told the shape (`app_doctor_ai.VERDICT_SHAPE`) rather than
    handed a schema flag: a scheduled task is an ordinary session, and the
    server reads the file back leniently (`_parse_verdict`) — a verdict that
    does not read is reported as "no verdict", not trusted. The checksum the
    row is cached on is never in the prompt: the server computed it from the
    same files before the task existed, and the session has no say in it.

    `.fused/` is gitignored for every app (`app_git._GITIGNORE`), so the
    verdict never lands in the commit the finished turn is recorded as."""
    from fused_render import app_doctor_ai

    entry_name = os.path.basename(entry_html)
    listed = "\n".join(f"- {rel}" for rel in files) or "- (no .html/.css/.js/.svg files found)"
    return (
        f"{DOCTOR_PROMPT_PREFIX}{_CHECK_TASK_MARK}{app_doctor_ai.CHECK_ID}` "
        f"(`{entry_name}` is its entry page). This is a READ-ONLY check, not a fix: "
        f"do not edit, create, move or commit any file except the one verdict file "
        f"named below.\n\n"
        f"Invoke the `fused-render:{app_doctor_ai.RUBRIC_SKILL}` skill and judge ONLY "
        f"by its trap table, for DESKTOP browsers only — Chrome/Edge, Firefox, Safari "
        f"and the macOS WKWebView. Ignore phones and tablets entirely: nothing about "
        f"touch, viewport zoom or mobile layout is a finding. A finding is a concrete "
        f"line in one of the files below that will look or behave differently in one "
        f"of those engines, with no fallback (`@supports`, or the `-webkit-` twin "
        f"beside it). Nothing beyond the table; nothing about logic, style or "
        f"performance. Few, certain findings; a problem repeated across lines is ONE "
        f"finding at its first occurrence. Write for the app's author, who may not "
        f"know CSS.\n\n"
        f"Files to read (relative to the app folder; read nothing else):\n{listed}\n\n"
        f"When you have judged every file, write your verdict as JSON to exactly this "
        f"path, creating parent folders if needed, overwriting whatever is there:\n"
        f"`{verdict_path}`\n\nShape:\n{app_doctor_ai.VERDICT_SHAPE}\n\n"
        f"Then, in the chat, reply with the summary sentence followed by one short "
        f"line per finding — plain words for the app's author, what a visitor would "
        f"notice and in which browser, no code, no file paths, no CSS terms. Nothing "
        f"else, and nothing at all under the findings if there were none. Do not commit."
    )
