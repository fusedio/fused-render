"""The App Doctor report, and the task that explains and fixes it.

Sharing an app is the moment its folder stops being private: a key pasted
into a `.py` while wiring something up, a path that only resolves on the
machine it was written on, a `__pycache__` swept along, an entry page still
declaring a fused API version the runtime stopped speaking. None of that is
visible from the outside, and all of it is deterministic to check — so the
app page and the explorer's entry-page header offer one button that runs the
checks and shows them as a checklist.

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
* `pyproject.toml` and `icon.svg` parsing, when either is there at all, and
* whether the folder's own git repo has everything committed.

EVERY CHECK IS DETERMINISTIC. Nothing here forms an opinion — a check either
found a shape or it did not, and a "fail" is a fact plus the lines it sits on.
The opinion is the fix task's, and the fix task is a Claude session running
the skill: `doctor_prompt` is one line that invokes it, the same seam the
migration task uses (`fused_api_version.migration_prompt`).

Never raises. A doctor that crashes on the app it was asked to examine is
worse than no doctor, so every check that touches the filesystem or a
subprocess degrades to `skip` with the reason in `detail`.
"""
import importlib.util
import os

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


def _check(cid: str, label: str, state: str, detail: str,
           findings: list | None = None) -> dict:
    return {
        "id": cid,
        "label": label,
        "state": state,
        "detail": detail,
        "findings": findings or [],
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


def _git_pending(app_dir: str) -> tuple[str, list[str]]:
    """`(state, paths)` for the folder's version control: FAIL with the
    uncommitted paths, PASS when the folder is clean, SKIP when git cannot
    answer (not a repo, git missing, a repo that will not read).

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
    # `git status` reports paths relative to the REPO root, which for an app in
    # the shared repo is one level up from the folder being reviewed. Strip that
    # prefix so a row reads like every other finding's path — app-relative.
    prefix = os.path.basename(app_dir.rstrip("/\\")) + "/"
    out = []
    for ln in lines:
        code, _, rest = ln[:2], ln[2:3], ln[3:]
        rest = rest.strip().strip('"')
        if rest.startswith(prefix):
            rest = rest[len(prefix):]
        out.append(f"{code.strip() or '??'} {rest}")
    return FAIL, out


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


def report(app_dir: str) -> dict:
    """The whole checklist for one app folder.

    `{"path", "entry", "ok", "checks": [...]}`. `ok` is "nothing failed" —
    a skipped check is not a pass, but it is not a problem to report either.
    """
    app_dir = os.path.abspath(app_dir)
    checks: list[dict] = []

    try:
        entry = app_listing.app_entry(app_dir)
    except OSError:
        entry = None
    checks.append(_check(
        "entry", "Has an app entry page", PASS if entry else FAIL,
        f"{os.path.basename(entry)} carries the fused-app marker" if entry
        else 'no page in this folder carries <meta name="fused-app"> — '
             "without one there is nothing for whoever you share it with to open",
    ))

    # The declared API version. Only askable of an entry, and only meaningful
    # when the runtime knows its own version (no migration docs = version 0,
    # and nothing is behind 0).
    current = fused_api_version.current_version()
    if entry is None:
        checks.append(_check("api-version", "Declares the current fused API version",
                             SKIP, "no entry page to read the version tag off"))
    elif current <= 0:
        checks.append(_check("api-version", "Declares the current fused API version",
                             SKIP, "this runtime does not report a fused API version"))
    else:
        declared = fused_api_version.api_version(entry)
        behind = declared < current
        checks.append(_check(
            "api-version", "Declares the current fused API version",
            FAIL if behind else PASS,
            f"declares version {declared}; the runtime is on {current} — "
            f"the `fused` calls in this app may have moved since"
            if behind else f"version {current}, the one the runtime speaks",
        ))

    # The floor engine's two content families.
    eng = engine()
    if eng is None:
        why = engine_error() or "the check engine is unavailable"
        for cid, label, _prefix, _clean in _FAMILY_CHECKS:
            checks.append(_check(cid, label, SKIP, why))
    else:
        why = ""
        try:
            findings = eng.check(app_dir)
        except Exception as exc:  # noqa: BLE001 — `check` promises not to raise; trust nothing
            findings = None
            why = f"the check engine failed: {exc}"
        for cid, label, prefix, clean in _FAMILY_CHECKS:
            if findings is None:
                checks.append(_check(cid, label, SKIP, why))
                continue
            hits = [f for f in findings if str(f.get("rule", "")).startswith(prefix)]
            hits.sort(key=lambda f: (f.get("path", ""), f.get("line", 0), f.get("rule", "")))
            checks.append(_check(
                cid, label, FAIL if hits else PASS,
                f"{len(hits)} line{'' if len(hits) == 1 else 's'} to look at"
                if hits else clean,
                hits,
            ))

    # Generated state loose in the tree.
    stray = _generated_paths(app_dir)
    checks.append(_check(
        "generated", "No generated files outside .fused/",
        FAIL if stray else PASS,
        f"{len(stray)} generated path{'' if len(stray) == 1 else 's'} in the app "
        "tree — delete them, move them under .fused/, or gitignore them"
        if stray else "no caches or build artifacts loose in the folder",
        [{"rule": "generated:stray", "path": p, "line": 0, "excerpt": p} for p in stray],
    ))

    # Structure: the two files that make a share recognizable.
    try:
        names = os.listdir(app_dir)
    except OSError:
        names = []
    has_readme = any(n.lower().startswith("readme")
                     and os.path.isfile(os.path.join(app_dir, n)) for n in names)
    checks.append(_check(
        "readme", "Has a README", PASS if has_readme else FAIL,
        "a README says what this is" if has_readme
        else "no README — say what this app does for whoever you share it with",
    ))

    preview = os.path.join(app_dir, app_listing.PREVIEW_IMAGE_NAME)
    try:
        has_preview = os.path.isfile(preview) and os.path.getsize(preview) > 0
    except OSError:
        has_preview = False
    checks.append(_check(
        "preview", "Has a preview.png thumbnail", PASS if has_preview else FAIL,
        "preview.png is what a card shows" if has_preview
        else f"no {app_listing.PREVIEW_IMAGE_NAME} (or it is empty) — this is how "
             "the app is recognized in a grid of others",
    ))

    # Two optional files, whose whole check is that they parse. Absent is not
    # a finding: an app is not obliged to have either.
    for cid, name, kind, label in (
        ("pyproject", "pyproject.toml", "toml", "pyproject.toml parses"),
        ("icon", app_listing.ICON_NAME, "xml", "icon.svg parses"),
    ):
        path = os.path.join(app_dir, name)
        if not os.path.isfile(path):
            checks.append(_check(cid, label, SKIP, f"no {name} in this folder"))
            continue
        ok, reason = _parses(path, kind)
        checks.append(_check(cid, label, PASS if ok else FAIL,
                             f"{name} parses" if ok else f"{name}: {reason}"))

    # Version control.
    state, pending = _git_pending(app_dir)
    checks.append(_check(
        "git", "Everything committed", state,
        "this folder is not in a git repository this server can read" if state == SKIP
        else f"{len(pending)} uncommitted path{'' if len(pending) == 1 else 's'} — "
             "commit them so what you share is what you tested" if state == FAIL
        else "the working tree is clean",
        [{"rule": "git:uncommitted", "path": p, "line": 0, "excerpt": p}
         for p in pending],
    ))

    return {
        "path": app_dir,
        "entry": entry,
        "ok": not any(c["state"] == FAIL for c in checks),
        "checks": checks,
    }


# ------------------------------------------------------------------- the task

# The first words of every App Doctor task's prompt — how a stored task is
# recognised as one (`server/routers/apps.py`) without a second field on the
# schedule entry, the same trick the migration task uses.
DOCTOR_PROMPT_PREFIX = "Run App Doctor on this fused-render app"


def is_doctor_prompt(message: str) -> bool:
    return str(message or "").startswith(DOCTOR_PROMPT_PREFIX)


def doctor_prompt(entry_html: str) -> str:
    """The fix task's text: invoke the skill, name the entry page. The skill
    carries the checks, the judgment about which hits are real, and where each
    kind of fix belongs — repeating any of that here would be the second copy
    the skill exists to prevent."""
    entry_name = os.path.basename(entry_html)
    return (
        f"{DOCTOR_PROMPT_PREFIX} (`{entry_name}` is its entry page). Invoke the "
        f"`{SKILL_QUALIFIED}` skill and follow it end to end. Then, for each "
        f"finding: say in one line what it is and why it matters, and fix the "
        f"ones that are safe to fix here — a hardcoded path, stray generated "
        f"files, a missing README or a stale `fused-api-version` tag (route that "
        f"last one through the skill it names). For a leaked credential, say "
        f"where it is and that it needs rotating; do not move the value "
        f"somewhere else and call it fixed. Leave everything the skill does not "
        f"ask about alone."
    )
