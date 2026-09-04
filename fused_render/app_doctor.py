"""App Doctor: one engine that reads an app folder and reports what would
embarrass its author — a leaked key, a path that only resolves on this
machine, and basic housekeeping.

Judging whether an app's `fused.*` calls are actually correct is not this
engine's job any more — that used to be a hand-maintained set of known
member names, which could only ever ask "does this name exist", not "is it
being used correctly", and a code review found it firing HIGH on
`render.fused.io` inside an ordinary link. That judgment now belongs
to the skill that authoritatively covers each API surface (fused-render-ai,
fused-render-authoring, fused-render-jobs, and so on) — see
skills/fused-render-app-doctor/SKILL.md's routing section. Detecting that a
page's declared `fused-api-version` is behind current stays here: it's a
version comparison, not a judgment call.

Two callers sit on top of `check()`: the `fused-render doctor` CLI (cli.py),
which a skill drives on request, and the community-apps repo's own CI
workflow, which runs it on every push. Both read the SAME findings, because
this module is the one place severity is decided — a caller that computed its
own idea of "bad" could disagree with the other, and a review that passes
locally but fails in CI (or the reverse) is worse than no review at all.

STDLIB ONLY, AND NO IMPORT FROM THE REST OF THE PACKAGE. Two reasons, not one:
first, this module is what the community-apps CI workflow runs on every push,
so a change anywhere else in fused_render — an added dependency, an import
that touches ~/.fused-render — can never make that workflow slower or make it
depend on more than "pip install fused-render" already installs. Second, a
handful of the checks below duplicate a RULE that lives elsewhere in the
package (which page is an app's entry, which file is its thumbnail, which API
version is current) rather than importing the function that already computes
it — deliberately: importing would pull in modules that read the live
workspace, spawn threads, or expect a running server, none of which exist in
a bare CI checkout of someone else's repo. The duplication is real and it is
the price of that isolation; the docstring on each check says which function
it mirrors, and a change there is a change to make here too.

FINDINGS ARE MASKED AT THE SOURCE. They end up in CI logs and chat
transcripts a person did not choose to keep secret, so `check()` never returns
an excerpt containing a whole matched secret — see `_mask` and the assertion
in tests/test_app_doctor.py. Every other family's excerpt is already safe
(a path, a member name, a missing filename) and is left as-is.

SEVERITY IS A PROPERTY OF THE RULE, NOT OF THE CALLER (see `_SEVERITY`). A
finding's `rule` is `"<family>:<check>"`; the family is everything before the
colon, and it is what both `--check` (fails a run on HIGH) and the human
report (grouped by family) read. HIGH is a secret or a device-specific path
— the two ways an app can leak something specific to the machine it was
written on. LOW is everything else: reported, and never a reason to fail a
run.

WORKING TREE ONLY. No history scan — a secret already committed is a job for
whatever gates publishing, not this engine, and scanning history would make
every run as slow as the app's oldest commit.
"""
import os
import re
import subprocess

# --------------------------------------------------------------- severity

# One table, read by both `check()`'s callers through `severity()` — see the
# module docstring for why this may not be decided twice.
_SEVERITY = {
    "secrets": "high",
    "device-path": "high",
    "structure": "low",
    "api-version": "low",
    "generated": "low",
}


def severity(rule: str) -> str:
    """The severity a `rule` string (`"<family>:<check>"`) carries. Unknown
    families read as "low" — a new check that forgets to register its family
    fails safe by never blocking a run, rather than blocking every run."""
    family = rule.split(":", 1)[0]
    return _SEVERITY.get(family, "low")


def _finding(rule: str, path: str, line: int, excerpt: str) -> dict:
    return {
        "rule": rule,
        "severity": severity(rule),
        "path": path,
        "line": line,
        "excerpt": excerpt,
    }


# --------------------------------------------------------- file enumeration

# The bookkeeping folders app_git.py's own _GITIGNORE keeps out of an app's
# history (see app_git.py's module docstring for why each one is there), plus
# node_modules — never worth reading AS CONTENT. Generated cache dirs
# (__pycache__ and friends) are deliberately NOT here: `_check_stray_files`
# needs to see them exist to flag them, so only their (binary, uninteresting)
# contents are skipped, via the same null-byte sniff every other file gets.
_IGNORED_DIR_NAMES = {".git", ".venv", ".fused", "node_modules"}
_IGNORED_FILE_SUFFIXES = (".html.json",)
_IGNORED_FILE_NAMES = {".claude-split.json", ".DS_Store"}
_IGNORED_FILE_PREFIXES = (".fused-render-write-probe.",)

# A file this large is never a source file worth scanning line-by-line, and
# reading it whole would make one bloated fixture (a vendored dataset, a
# checked-in model weight) dominate the cost of reviewing an entire app.
_MAX_BYTES_PER_FILE = 1_000_000

# A folder this large is not a small app someone is about to share; bounding
# the walk keeps a review of a huge, half-abandoned repo from hanging instead
# of reporting.
_MAX_FILES = 20_000


def _is_ignored_name(name: str) -> bool:
    return (name in _IGNORED_FILE_NAMES
            or name.endswith(_IGNORED_FILE_SUFFIXES)
            or name.startswith(_IGNORED_FILE_PREFIXES))


def _git_ls_files(app_dir: str) -> list[str] | None:
    """App-relative paths `git` itself considers part of the working tree —
    tracked files plus untracked-but-not-ignored ones — or None when `app_dir`
    is not inside a git repo (or `git` is not on PATH). Preferred over a walk
    whenever it is available: it is the same answer `git status` gives the
    author, so a file `.gitignore` already hides never becomes a finding."""
    try:
        r = subprocess.run(
            ["git", "-C", app_dir, "ls-files", "-z",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    names = [n for n in r.stdout.split(b"\0") if n]
    return [n.decode("utf-8", "replace") for n in names]


def _walk_files(app_dir: str) -> list[str]:
    """App-relative paths found by a bounded walk, for an app folder that is
    not (or not yet) a git repo. Honours the same ignore names `git ls-files`
    would have hidden via app_git.py's own .gitignore, so the two enumeration
    paths agree on what counts as app content."""
    out = []
    for root, dirs, files in os.walk(app_dir):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORED_DIR_NAMES
                          and not d.startswith("."))
        for name in sorted(files):
            if _is_ignored_name(name):
                continue
            rel = os.path.relpath(os.path.join(root, name), app_dir)
            out.append(rel.replace(os.sep, "/"))
            if len(out) >= _MAX_FILES:
                return out
    return out


def _candidate_files(app_dir: str) -> list[str]:
    """Every file `check()` should read, app-relative with `/` separators."""
    names = _git_ls_files(app_dir)
    if names is None:
        names = _walk_files(app_dir)
    else:
        names = [n for n in names if not _is_ignored_name(os.path.basename(n))]
    return sorted(names)


def _read_text(app_dir: str, rel_path: str) -> str | None:
    """`rel_path`'s content as text, or None when it looks binary, is too
    large, or can't be read. A null byte in the first chunk is the same
    sniff `git` itself uses to call a file binary; it is cheap and it is
    enough — this engine never needs to be exactly right about encoding,
    only to avoid choking on a binary asset sitting in the app folder."""
    path = os.path.join(app_dir, rel_path)
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
            if b"\0" in head:
                return None
            rest = fh.read(_MAX_BYTES_PER_FILE - len(head))
        raw = head + rest
    except OSError:
        return None
    return raw.decode("utf-8", "replace")


# ------------------------------------------------------------------ secrets

# Recognisable formats: a prefix (or shape) that is, on its own, strong
# evidence of a real credential rather than a coincidence of naming. Each
# pattern's own group 0 is what gets masked and reported — no capture groups,
# so `_mask` always has the whole match to work with.
_PREFIXED_SECRET_PATTERNS = {
    "aws-access-key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "github-token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{36,}"),
    "slack-token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "anthropic-key": re.compile(rb"sk-ant-[A-Za-z0-9\-_]{20,}"),
    "openai-key": re.compile(rb"sk-[A-Za-z0-9]{20,}"),
    "google-api-key": re.compile(rb"AIza[0-9A-Za-z\-_]{35}"),
    "stripe-key": re.compile(rb"sk_live_[0-9a-zA-Z]{24,}"),
}

# A PEM block. Reported whole (well, masked whole) rather than per-line: a
# private key split across an excerpt would still be a private key.
_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# `NAME = "value"` / `NAME: "value"` where NAME reads as a credential and
# value is not obviously a placeholder. This is the catch-all for the
# provider-specific formats above having no fixed shape at all — a database
# password, an internal service token — so it is deliberately looser, and
# deliberately excludes anything that reads like a stand-in for a real value.
_ASSIGNMENT_SECRET_RE = re.compile(
    rb"(?i)\b([A-Z0-9_]*(?:SECRET|API[_-]?KEY|ACCESS[_-]?KEY|TOKEN|PASSWORD"
    rb"|PASSWD|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*"
    rb"[\"']([^\"'\r\n]{8,})[\"']"
)

_PLACEHOLDER_RE = re.compile(
    rb"(?i)^(x+|\*+|\.+|_+|-+|<[^>]*>|\{[^}]*\}|\$\{[^}]*\}|%[a-z_]+%"
    rb"|(your|my|insert|enter|add|replace|change|fill).*"
    rb"|(fake|dummy|sample|example|placeholder|redacted|todo|changeme"
    rb"|xxxxxxxx|none|null|undefined|test).*)$"
)


def _is_placeholder(value: bytes) -> bool:
    return bool(_PLACEHOLDER_RE.match(value.strip()))


def _mask(secret: bytes) -> str:
    """`secret`, with the middle blacked out and never the whole thing shown.

    Asserted directly by tests/test_app_doctor.py: no finding's excerpt may
    ever contain a whole matched secret, because these land in CI logs and
    chat transcripts. A short secret (8 chars or fewer) is masked entirely —
    there is no way to show a fragment of something that short without
    showing most of it.
    """
    if len(secret) <= 8:
        return "*" * len(secret)
    keep = 2
    return (secret[:keep] + b"*" * (len(secret) - 2 * keep) + secret[-keep:]).decode(
        "ascii", "replace")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _check_secrets(rel_path: str, text: str, findings: list) -> None:
    data = text.encode("utf-8", "replace")

    for name, pattern in _PREFIXED_SECRET_PATTERNS.items():
        for m in pattern.finditer(data):
            findings.append(_finding(
                f"secrets:{name}", rel_path, _line_of(text, m.start()),
                _mask(m.group(0)),
            ))

    for m in _PRIVATE_KEY_RE.finditer(data):
        findings.append(_finding(
            "secrets:private-key", rel_path, _line_of(text, m.start()),
            _mask(m.group(0)),
        ))

    for m in _ASSIGNMENT_SECRET_RE.finditer(data):
        value = m.group(2)
        if _is_placeholder(value):
            continue
        findings.append(_finding(
            "secrets:assignment", rel_path, _line_of(text, m.start()),
            f"{m.group(1).decode()} = {_mask(value)}",
        ))


# ------------------------------------------------------------ device paths

# Absolute paths that are true statements about ONE machine: a home
# directory (whoever's — the folder is meant to move between machines and
# users), or a handful of OS-level roots that mean "this filesystem, laid
# out exactly like mine". A relative path, or an absolute path inside the
# app folder itself, says nothing about the machine it runs on next.
_DEVICE_ROOTS = (
    "/home/", "/Users/", "/root/", "/opt/", "/var/", "/tmp/",
    "/mnt/", "/media/", "/Volumes/", "/private/",
)
_POSIX_DEVICE_RE = re.compile(
    "(?:" + "|".join(re.escape(r) for r in _DEVICE_ROOTS) + r")[^\s\"'<>)]*"
)
_WIN_DEVICE_RE = re.compile(
    r"[A-Za-z]:[\\/](?:Users|Windows|Program Files(?: \(x86\))?|ProgramData)"
    r"[\\/][^\s\"'<>)]*",
    re.IGNORECASE,
)


def _check_device_paths(rel_path: str, text: str, findings: list) -> None:
    for pattern in (_POSIX_DEVICE_RE, _WIN_DEVICE_RE):
        for m in pattern.finditer(text):
            findings.append(_finding(
                "device-path:hardcoded", rel_path, _line_of(text, m.start()),
                m.group(0),
            ))


# ---------------------------------------------------------------- structure

# The one authored thumbnail name — the same exact-listdir match
# app_listing.app_preview_image makes, and for the same reason (case-folding
# is not portable across filesystems, so a membership test is the only rule
# every machine agrees on).
_PREVIEW_IMAGE_NAME = "preview.png"
_ICON_NAME = "icon.svg"


def _has_fused_meta(html_path: str) -> bool:
    """Same head-budget sniff as app_listing.has_fused_meta: the marker sits
    in the first few KiB of <head>, so a whole-file read per candidate page
    is never needed."""
    try:
        with open(html_path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return re.search(rb'<meta\s[^>]*name\s*=\s*["\']?fused-app["\']?[^>]*>',
                      head, re.IGNORECASE) is not None


def _direct_child_pages(app_dir: str) -> list[str]:
    """Non-hidden top-level `.html` files, the same population
    `app_listing.app_entry` scans for the one that is the entry."""
    try:
        names = sorted(os.listdir(app_dir))
    except OSError:
        return []
    return [n for n in names if not n.startswith(".") and n.lower().endswith(".html")
            and os.path.isfile(os.path.join(app_dir, n))]


def _app_entry(app_dir: str) -> str | None:
    """Mirrors app_listing.app_entry: the first non-hidden top-level `.html`
    (name order) carrying the app marker, or None. Duplicated rather than
    imported — see the module docstring."""
    for name in _direct_child_pages(app_dir):
        if _has_fused_meta(os.path.join(app_dir, name)):
            return name
    return None


def _check_structure(app_dir: str, findings: list) -> None:
    entry = _app_entry(app_dir)
    if entry is None:
        findings.append(_finding(
            "structure:no-entry", ".", 0,
            "no top-level .html page carries <meta name=\"fused-app\">",
        ))

    icon = os.path.join(app_dir, _ICON_NAME)
    if os.path.isfile(icon):
        try:
            import xml.etree.ElementTree as ET
            ET.parse(icon)
        except Exception:
            findings.append(_finding(
                "structure:bad-icon", _ICON_NAME, 0,
                f"{_ICON_NAME} does not parse as XML",
            ))

    try:
        has_readme = any(
            n.lower().startswith("readme") and os.path.isfile(os.path.join(app_dir, n))
            for n in os.listdir(app_dir)
        )
    except OSError:
        has_readme = True  # an unlistable folder is not a "missing README" finding
    if not has_readme:
        findings.append(_finding(
            "structure:missing-readme", ".", 0, "no README in the app folder",
        ))

    pyproject = os.path.join(app_dir, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            import tomllib
            with open(pyproject, "rb") as fh:
                tomllib.load(fh)
        except Exception as exc:
            findings.append(_finding(
                "structure:bad-pyproject", "pyproject.toml", 0,
                f"pyproject.toml does not parse: {exc}",
            ))

    try:
        has_preview = (_PREVIEW_IMAGE_NAME in os.listdir(app_dir)
                       and os.path.getsize(
                           os.path.join(app_dir, _PREVIEW_IMAGE_NAME)) > 0)
    except OSError:
        has_preview = False
    if not has_preview:
        findings.append(_finding(
            "structure:missing-thumbnail", ".", 0,
            f"no {_PREVIEW_IMAGE_NAME} thumbnail (or it is empty)",
        ))


# ------------------------------------------------------------- API version

# Mirrors fused_api_version.version_from_text / current_version, without
# importing that module (see the module docstring) — current_version reads
# the migration skill's docs/ folder through skill_sources, which is not
# stdlib. This constant is the highest API version THIS copy of the engine
# knows about; bumping it is part of shipping a new version, the same way
# the starter's own tag is bumped alongside a new docs/vN.md (see
# fused_api_version's docstring) — the two already have to move together,
# this is a third place that moves with them.
CURRENT_API_VERSION = 1

_API_VERSION_TAG_RE = re.compile(
    rb'<meta\s[^>]*name\s*=\s*["\']?fused-api-version["\']?[^>]*>',
    re.IGNORECASE)
_API_VERSION_CONTENT_RE = re.compile(rb'content\s*=\s*["\']?\s*(\d+)\s*["\']?',
                                     re.IGNORECASE)


def _declared_api_version(html_path: str) -> int:
    try:
        with open(html_path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return 0
    tag = _API_VERSION_TAG_RE.search(head)
    if not tag:
        return 0
    m = _API_VERSION_CONTENT_RE.search(tag.group(0))
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def _check_api_version(app_dir: str, findings: list) -> None:
    entry = _app_entry(app_dir)
    if entry is None:
        return  # "no entry" is structure:no-entry's finding to make, not this one's
    declared = _declared_api_version(os.path.join(app_dir, entry))
    if declared < CURRENT_API_VERSION:
        findings.append(_finding(
            "api-version:behind", entry, 0,
            f"declares API version {declared}, current is {CURRENT_API_VERSION}",
        ))


# ------------------------------------------------------------ stray files

# Generated artifacts that belong in the app's own machine-local .fused/
# folder (see app_git.py's module docstring for why that folder is never
# app history) but are loose in the tree instead — almost always a cache or
# a log a run left beside the code rather than under it.
_STRAY_SUFFIXES = (".pyc", ".log", ".sqlite", ".sqlite3", ".db")
_STRAY_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _check_stray_files(candidates: list[str], findings: list) -> None:
    for rel in candidates:
        parts = rel.split("/")
        if any(p in _STRAY_DIR_NAMES for p in parts[:-1]):
            findings.append(_finding(
                "generated:stray-file", rel, 0,
                f"{parts[-2]}/ is a generated cache, not app content",
            ))
            continue
        if rel.endswith(_STRAY_SUFFIXES):
            findings.append(_finding(
                "generated:stray-file", rel, 0,
                f"{rel} looks generated (belongs in .fused/, or shouldn't be committed)",
            ))


# --------------------------------------------------------------------- API


def check(app_dir: str) -> list[dict]:
    """Every finding for `app_dir`: `{rule, severity, path, line, excerpt}`,
    `path` always relative to `app_dir` (`.` for a folder-level finding with
    no single file to point at, `line` 0 the same way). Never raises — an
    unreadable app is one the caller already knows is broken some other
    way, and a doctor that crashes on the app it was asked to examine is not
    useful to anyone.

    Reviews exactly one app folder — `app_dir` itself — and nothing else.
    A caller that wants every app in a repo reviewed runs this once per app
    folder; see skills/fused-render-app-doctor/ci/app-check.yml for that
    loop."""
    app_dir = os.path.abspath(app_dir)

    findings: list[dict] = []

    candidates = _candidate_files(app_dir)
    for rel in candidates:
        text = _read_text(app_dir, rel)
        if text is None:
            continue
        _check_secrets(rel, text, findings)
        _check_device_paths(rel, text, findings)

    _check_structure(app_dir, findings)
    _check_api_version(app_dir, findings)
    _check_stray_files(candidates, findings)

    return findings
