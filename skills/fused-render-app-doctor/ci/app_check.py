"""A floor engine that reads an app folder and flags the obvious problems
worth failing a push over — a leaked credential, a path that only resolves
on the machine it was written on, and an app folder missing the three files
that make a share openable and recognizable to whoever receives it:
`index.html`, a README, and a `preview.png` thumbnail.

Judging whether an app's `fused.*` calls are actually correct is not this
script's job: that question needs to know whether a name is being used
correctly, not just whether it exists, and telling those apart is a job for
the skill that reads and greps the app directly
(skills/fused-render-app-doctor/SKILL.md) rather than a fixed pattern match.

This script is a file the fused-render-app-doctor skill writes, verbatim,
into whatever repo an app lives in, at `.github/app_check.py`, alongside the
workflow at `.github/workflows/app-check.yml` that runs it on every push (see
that skill's SKILL.md for the setup procedure and app-check.yml for the loop
over a repo's app folders). Its job is a floor that fails a push on an
obvious problem — a strict, deliberate SUBSET of what the skill's own review
covers, not a substitute for it.

STDLIB ONLY. It runs on every push in a plain GitHub Actions runner, with
nothing installed beyond the standard library — no import can add a
dependency, a network call, or a wait for anything to install.

FINDINGS ARE MASKED AT THE SOURCE. They end up in CI logs and chat
transcripts a person did not choose to keep secret, so `check()` never
returns an excerpt containing a whole matched secret — see `_mask` and the
assertion in tests/test_app_doctor.py. A device path or a structural gap is
left as-is: neither is a secret, and showing it whole is what makes the
finding actionable.

EVERY FINDING FAILS THE RUN. All three families this script reports —
secrets, device paths, and structure — are the kind of problem a CI floor
exists to catch before it reaches someone else's machine, so `SEVERITY`
is a flat "high" rather than a per-family table: there is no third tier here
that is worth reporting but never worth failing a run over. A finding that
was never going to fail anything belongs in the skill's judgment-driven
review, not in this script.

WORKING TREE ONLY. No history scan — a secret already committed is a job for
whatever gates publishing, not this script, and scanning history would make
every run as slow as the app's oldest commit.

Run as `python app_check.py [path]` (path defaults to `.`): prints findings
grouped by family, then exits 1 if any fired and 0 if the folder is clean.
"""
import os
import re
import subprocess
import sys

# --------------------------------------------------------------- severity

# Every family `check()` runs is HIGH: each is the kind of problem a CI floor
# exists to fail a push over, so there is no second tier here that is worth
# reporting but never worth failing a run over (see the module docstring).
# `severity` is exported (rather than inlining the constant into `_finding`)
# because `main` reads it too, for the same "does this finding fail the run"
# question the grouped report answers.
SEVERITY = "high"


def _finding(rule: str, path: str, line: int, excerpt: str) -> dict:
    return {
        "rule": rule,
        "severity": SEVERITY,
        "path": path,
        "line": line,
        "excerpt": excerpt,
    }


# --------------------------------------------------------- file enumeration

# The bookkeeping folders app_git.py's own _GITIGNORE keeps out of an app's
# history (see app_git.py's module docstring for why each one is there), plus
# node_modules — never worth reading AS CONTENT.
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


def _has_ignored_dir_component(rel_path: str) -> bool:
    """True when any directory this path sits under is bookkeeping rather
    than app content — one of `_IGNORED_DIR_NAMES`, or hidden (a dot-prefixed
    name, the same rule `_walk_files` already applies while walking). Applying
    it to the git-listed path too (not just the walk) matters here
    specifically: `.github` is a dot-prefixed dir, and the workflow this
    script runs from — and a committed copy of this very file — live there.
    Without this, a repo whose app IS the repo root scans its own CI setup
    and flags this file's regex literals as leaked secrets."""
    parts = rel_path.split("/")[:-1]
    return any(p in _IGNORED_DIR_NAMES or p.startswith(".") for p in parts)


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
        names = [n for n in names if not _is_ignored_name(os.path.basename(n))
                  and not _has_ignored_dir_component(n)]
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
#
# Two alternatives for the value: quoted (Python, JS, JSON, TOML — anything
# where the assignment sits inside source syntax) or bare (`.env` files and
# `docker-compose.yml`-style `KEY=value` lines, which are never quoted and
# are among the likeliest places in a tree to hold a real credential). The
# bare alternative stops at whitespace/#/quote so it doesn't run past the end
# of the line into a trailing comment.
_ASSIGNMENT_SECRET_RE = re.compile(
    rb"(?i)\b([A-Z0-9_]*(?:SECRET|API[_-]?KEY|ACCESS[_-]?KEY|TOKEN|PASSWORD"
    rb"|PASSWD|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*"
    rb"(?:[\"']([^\"'\r\n]{8,})[\"']|([^\s\"'#\r\n]{8,}))"
)

# A value made of nothing but one repeated filler character or a template
# marker — "xxxxxxxx", "********", "<your-key>", "${API_KEY}", "%API_KEY%" —
# is a placeholder outright, whole-string.
_PLACEHOLDER_SYMBOL_RE = re.compile(
    rb"(?i)^(x+|\*+|\.+|_+|-+|<[^>]*>|\{[^}]*\}|\$\{[^}]*\}|%[a-z_]+%)$"
)

# Otherwise, a value is a placeholder only when EVERY word in it (splitting
# on runs of non-alphanumeric characters — hyphens, underscores, dots) is
# drawn from this list of generic filler. A value that merely BEGINS with
# one of these words is not enough: "mysql-prod-9f3k2xyz" splits into
# "mysql", "prod", "9f3k2xyz", and "mysql" itself is not "my" — the first
# word fails to be filler, so the whole value is judged a real secret. The
# same holds for "testkey-prod-abc123456" ("testkey" is not "test") and
# "nonesuch-real-token-value" ("nonesuch" is not "none", and "real" is not
# filler at all).
_PLACEHOLDER_WORDS = {
    "your", "my", "insert", "enter", "add", "replace", "change", "changeme",
    "fake", "dummy", "sample", "example", "placeholder", "redacted", "todo",
    "none", "null", "undefined", "test", "api", "key", "secret", "token",
    "password", "passwd", "credential", "here", "please", "value", "string",
    "me",
}
_WORD_RE = re.compile(r"[^a-z0-9]+")


def _is_placeholder(value: bytes) -> bool:
    stripped = value.strip()
    if _PLACEHOLDER_SYMBOL_RE.match(stripped):
        return True
    words = [w for w in _WORD_RE.split(stripped.decode("utf-8", "replace").lower()) if w]
    return bool(words) and all(w in _PLACEHOLDER_WORDS for w in words)


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

    def line_for(byte_offset: int) -> int:
        # `data` is UTF-8 bytes but `text` (and `_line_of`) is a `str`; a
        # byte offset counted against the character-indexed string over-counts
        # by however many bytes multi-byte characters before it add, so
        # decode only the prefix and count newlines in THAT.
        return _line_of(data[:byte_offset].decode("utf-8", "replace"), byte_offset)

    for name, pattern in _PREFIXED_SECRET_PATTERNS.items():
        for m in pattern.finditer(data):
            findings.append(_finding(
                f"secrets:{name}", rel_path, line_for(m.start()),
                _mask(m.group(0)),
            ))

    for m in _PRIVATE_KEY_RE.finditer(data):
        findings.append(_finding(
            "secrets:private-key", rel_path, line_for(m.start()),
            _mask(m.group(0)),
        ))

    for m in _ASSIGNMENT_SECRET_RE.finditer(data):
        value = m.group(2) if m.group(2) is not None else m.group(3)
        if _is_placeholder(value):
            continue
        findings.append(_finding(
            "secrets:assignment", rel_path, line_for(m.start()),
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
# The match body itself: a root, then at least one more path-shaped segment
# of ordinary filename characters.
_POSIX_DEVICE_RE = re.compile(
    "(?:" + "|".join(re.escape(r) for r in _DEVICE_ROOTS) + r")"
    + r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*"
)
_WIN_DEVICE_RE = re.compile(
    r"[A-Za-z]:[\\/](?:Users|Windows|Program Files(?: \(x86\))?|ProgramData)"
    r"[\\/][^\s\"'<>)]*",
    re.IGNORECASE,
)


def _preceded_by_url_host(text: str, match_start: int) -> bool:
    """True when `text` just before `match_start` is a URL scheme/host
    boundary (`scheme://host` or `scheme://host:port`) leading straight into
    the match — the case where the match is a URL's path component, sharing
    a root's spelling without saying anything about a local filesystem."""
    window_start = max(0, match_start - 256)
    prefix = text[window_start:match_start]
    return re.search(r"://[A-Za-z0-9.\-]+(?::\d+)?$", prefix) is not None


def _quote_precedes(text: str, match_start: int) -> bool:
    """True when the character right before `match_start` is a quote —
    the shape an actual filesystem path takes in source (an assigned string,
    an HTML attribute, a fenced code span), as opposed to a bare word inside
    a sentence of prose, which this engine leaves alone."""
    return match_start > 0 and text[match_start - 1] in "\"'`"


def _check_device_paths(rel_path: str, text: str, findings: list) -> None:
    for m in _POSIX_DEVICE_RE.finditer(text):
        if _preceded_by_url_host(text, m.start()) or not _quote_precedes(text, m.start()):
            continue
        findings.append(_finding(
            "device-path:hardcoded", rel_path, _line_of(text, m.start()),
            m.group(0),
        ))
    for m in _WIN_DEVICE_RE.finditer(text):
        if not _quote_precedes(text, m.start()):
            continue
        findings.append(_finding(
            "device-path:hardcoded", rel_path, _line_of(text, m.start()),
            m.group(0),
        ))


# ---------------------------------------------------------------- structure

# The three files that make a shared app openable and recognizable to
# whoever receives it: a page to open, a README to say what it is, and a
# thumbnail to show in a grid of other apps. Plain existence (and, for the
# thumbnail, non-emptiness) — this engine does not parse any of the three,
# it only asks whether the basics are there.
_ENTRY_NAME = "index.html"
_PREVIEW_IMAGE_NAME = "preview.png"


def _check_structure(app_dir: str, findings: list) -> None:
    if not os.path.isfile(os.path.join(app_dir, _ENTRY_NAME)):
        findings.append(_finding(
            "structure:missing-index", ".", 0,
            f"no {_ENTRY_NAME} — whoever you share this with needs a page to open",
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
            "structure:missing-readme", ".", 0,
            "no README in the app folder — say what this app does for whoever you share it with",
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
            f"no {_PREVIEW_IMAGE_NAME} thumbnail (or it is empty) — this is how "
            "the app is recognized in a grid of others",
        ))


# --------------------------------------------------------------------- API


def check(app_dir: str) -> list[dict]:
    """Every finding for `app_dir`: `{rule, severity, path, line, excerpt}`,
    `path` always relative to `app_dir`. Never raises — an unreadable app is
    one the caller already knows is broken some other way, and a doctor that
    crashes on the app it was asked to examine is not useful to anyone.

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

    return findings


def main(argv: list[str] | None = None) -> int:
    """Review one app folder and print findings grouped by family.

    The only mode is fail-on-finding: a CI floor has no use for a run that
    reports a leaked credential and still exits 0, so there is no flag to
    turn that off. Returns the process exit code rather than calling
    `sys.exit` itself, so a caller in the same process can inspect it."""
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else "."
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        print(f"{path} is not a directory — nothing to review", file=sys.stderr)
        return 2

    findings = check(path)
    if not findings:
        print(f"no findings — {path} looks clean")
        return 0

    by_family: dict[str, list] = {}
    for f in findings:
        by_family.setdefault(f["rule"].split(":", 1)[0], []).append(f)
    for family in sorted(by_family):
        group = by_family[family]
        label = group[0]["severity"].upper()
        print(f"\n{family} ({label}):")
        for f in group:
            where = f["path"] if not f["line"] else f"{f['path']}:{f['line']}"
            print(f"  {where}  {f['excerpt']}")
    print(f"\n{len(findings)} finding(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
