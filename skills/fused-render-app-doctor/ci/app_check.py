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

NOT EVERY FINDING FAILS THE RUN — CANDIDATES REPORT, FACTS FAIL. This script
used to treat every finding as the same flat "high" severity, on the theory
that everything it reports is worth failing a push over. That theory does not
survive contact with a real workspace: a run of this exact engine over the 8
apps in a real `~/Fused/local` produced 40 content findings and every single
one was a false positive — 26 were the same absolute path repeated across
committed `runs/*.json` logs, 6 were `/tmp/xxx` inside a vendored stdlib
module's docstrings, 5 were paths inside markdown code spans (a backtick used
to count as an opening quote), 2 were a deliberate `SKIP_DIRS` constant and a
relative URL in test HTML, and 1 was a fixture's obviously-fake password. The
secrets and device-path families are pattern matches over arbitrary text —
they locate CANDIDATES, and only a read of the surrounding file decides
whether one is real, which is exactly the judgment `SKILL.md` exists to make.
Failing a push on a candidate alone means a false positive blocks a real
commit; that cost model is upside down for these two families.
`CHECK_META` (below) marks `secrets` and `device-paths` as `kind: "candidate"`
and everything else — the three structure gaps — `kind: "fact"`: a missing
`index.html`/README/`preview.png` is not a pattern match over prose, it is a
direct `os.path.isfile` answer with no false-positive rate at all. `main`
exits 1 only when a fact finding fired; a run with candidates only prints them
and exits 0, so someone still sees them without a push getting blocked over
a maybe.

WORKING TREE ONLY. No history scan — a secret already committed is a job for
whatever gates publishing, not this script, and scanning history would make
every run as slow as the app's oldest commit.

Run as `python app_check.py [path]` (path defaults to `.`): prints one
`path:line: rule: excerpt` line per finding, then exits 1 if any FACT finding
fired and 0 otherwise (a clean folder, or one with candidates only).
"""
import fnmatch
import os
import re
import subprocess
import sys

# --------------------------------------------------------- severity, section, kind

# One entry per check id this script's two content families answer for, in the
# same vocabulary fused_render/app_doctor.py's checklist uses — `section`
# ("essentials" | "sharing"), `severity` ("critical" | "warning" | "suggested"),
# `kind` ("fact" | "candidate", see the module docstring for why that split
# exists). This is the SINGLE table for these two ids: app_doctor.py reads it
# rather than keeping a second copy, and a fix to one is a fix to both
# surfaces. The three structure ids (`entry`, `readme`, `preview`, keyed here
# by their full rule string rather than a family prefix — there is exactly one
# rule each) are not read by app_doctor.py, which computes those rows itself
# from the runtime's own knowledge; they are still needed here so `main` knows
# which findings are facts.
CHECK_META = {
    "secrets": ("essentials", "critical", "candidate"),
    "device-paths": ("sharing", "warning", "candidate"),
}
_STRUCTURE_META = {
    "structure:missing-index": ("essentials", "critical", "fact"),
    "structure:missing-readme": ("essentials", "suggested", "fact"),
    "structure:missing-thumbnail": ("sharing", "suggested", "fact"),
}


def _finding(rule: str, path: str, line: int, excerpt: str,
             section: str, severity: str, kind: str) -> dict:
    return {
        "rule": rule,
        "section": section,
        "severity": severity,
        "kind": kind,
        "path": path,
        "line": line,
        "excerpt": excerpt,
    }


def _family_finding(family: str, rule: str, path: str, line: int, excerpt: str) -> dict:
    """A finding for one of `CHECK_META`'s two ids — `secrets` or
    `device-paths` — reading that id's section/severity/kind from the one
    table rather than repeating them at each call site."""
    section, severity, kind = CHECK_META[family]
    return _finding(rule, path, line, excerpt, section, severity, kind)


def _structure_finding(rule: str, excerpt: str) -> dict:
    section, severity, kind = _STRUCTURE_META[rule]
    return _finding(rule, ".", 0, excerpt, section, severity, kind)


# --------------------------------------------------------- file enumeration

# The bookkeeping folders app_git.py's own _GITIGNORE keeps out of an app's
# history (see app_git.py's module docstring for why each one is there), plus
# node_modules — never worth reading AS CONTENT.
_IGNORED_DIR_NAMES = {".git", ".venv", ".fused", "node_modules", "__pycache__"}
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


class _GitignoreRule:
    """One non-blank, non-comment line of a `.gitignore`, in the subset that
    actually shows up in an app's ignore file: a plain name (`secrets.env`),
    a trailing-slash directory rule (`build/`), a glob (`*.log`), a rooted
    pattern (`/dist`), and a `!`-negation of any of those. Matching is always
    done relative to the directory the `.gitignore` itself sits in, the way
    git resolves it — `_is_gitignored` is what supplies that relative path."""

    __slots__ = ("pattern", "negate", "dir_only", "anchored")

    def __init__(self, raw: str):
        negate = raw.startswith("!")
        if negate:
            raw = raw[1:]
        dir_only = raw.endswith("/")
        if dir_only:
            raw = raw[:-1]
        # A pattern rooted with a leading slash, or carrying a slash anywhere
        # but at the end, only ever matches at the .gitignore's own level —
        # git does not let it match a same-named entry deeper in the tree.
        # Everything else (a bare name or glob with no interior slash) is a
        # basename rule: it matches that name at any depth.
        anchored = raw.startswith("/") or "/" in raw
        if raw.startswith("/"):
            raw = raw[1:]
        self.pattern = raw
        self.negate = negate
        self.dir_only = dir_only
        self.anchored = anchored

    def matches(self, local_rel: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        if self.anchored:
            return fnmatch.fnmatch(local_rel, self.pattern)
        name = local_rel.rsplit("/", 1)[-1]
        return fnmatch.fnmatch(name, self.pattern)


def _load_gitignore_rules(dir_abs: str) -> list[_GitignoreRule]:
    """Rules from the `.gitignore` directly inside `dir_abs`, or `[]` when
    there is none. A read failure (permissions, a symlink loop) is treated
    the same as "no rules here" — a folder that cannot be read cannot be
    trusted to say what it wants skipped, so this falls back to scanning
    rather than raising."""
    try:
        with open(os.path.join(dir_abs, ".gitignore"),
                   "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    rules = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rules.append(_GitignoreRule(line))
    return rules


def _is_gitignored(
    rel_path: str, is_dir: bool,
    scopes: list[tuple[str, list[_GitignoreRule]]],
) -> bool:
    """Whether `rel_path` (app-relative, `/`-separated) is ignored, applying
    each active `.gitignore` — outermost first — to the part of the path
    under its own directory, the way git layers nested ignore files. A
    scope's own rules are consulted in file order so a later `!`-negation
    within that same file overrides an earlier match, and a deeper
    `.gitignore` (later in `scopes`) has the final say over a shallower
    one."""
    ignored = False
    for scope_dir, rules in scopes:
        if scope_dir:
            prefix = scope_dir + "/"
            if rel_path == scope_dir:
                local_rel = ""
            elif rel_path.startswith(prefix):
                local_rel = rel_path[len(prefix):]
            else:
                continue
        else:
            local_rel = rel_path
        if not local_rel:
            continue
        for rule in rules:
            if rule.matches(local_rel, is_dir):
                ignored = not rule.negate
    return ignored


def _walk_files(app_dir: str) -> list[str]:
    """App-relative paths found by a bounded walk, for an app folder that is
    not (or not yet) a git repo. Honours the same ignore names `git ls-files`
    would have hidden via app_git.py's own .gitignore, plus whatever the
    app's own `.gitignore` file(s) say to skip — see `_is_gitignored` — so
    the two enumeration paths agree on what counts as app content whether or
    not the folder happens to be a git repo yet. An ignored directory is
    pruned outright rather than walked and filtered, so a large ignored tree
    (a `build/` full of generated output) costs nothing beyond the one
    `os.scandir` call that finds it."""
    app_dir = os.path.normpath(app_dir)
    out: list[str] = []

    def recurse(
        dir_abs: str, dir_rel: str,
        scopes: list[tuple[str, list[_GitignoreRule]]],
    ) -> bool:
        """Returns False once `_MAX_FILES` is hit, to unwind the recursion."""
        own_rules = _load_gitignore_rules(dir_abs)
        if own_rules:
            scopes = scopes + [(dir_rel, own_rules)]
        try:
            entries = sorted(os.scandir(dir_abs), key=lambda e: e.name)
        except OSError:
            return True
        for entry in entries:
            name = entry.name
            entry_rel = f"{dir_rel}/{name}" if dir_rel else name
            if entry.is_dir(follow_symlinks=False):
                if name in _IGNORED_DIR_NAMES or name.startswith("."):
                    continue
                if _is_gitignored(entry_rel, True, scopes):
                    continue
                if not recurse(entry.path, entry_rel, scopes):
                    return False
            else:
                if _is_ignored_name(name):
                    continue
                if _is_gitignored(entry_rel, False, scopes):
                    continue
                out.append(entry_rel)
                if len(out) >= _MAX_FILES:
                    return False
        return True

    recurse(app_dir, "", [])
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
# private key split across an excerpt would still be a private key. Group 1
# is the body alone (headers excluded) so a placeholder check can run on just
# the part that is supposed to be random — "BEGIN", "PRIVATE" and "KEY" are
# never going to be filler words, and checking the whole match against
# `_is_placeholder` would never recognise a placeholder PEM as one.
_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----(.*?)-----END [A-Z ]*PRIVATE KEY-----",
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
# are among the likeliest places in a tree to hold a real credential).
#
# The bare alternative has to be the whole of the rest of its line, give or
# take a trailing comment. Without that anchor it matches the leading run of
# any code expression assigned to a credential-shaped name, and the single
# most common such expression is the RIGHT way to hold a secret:
# `API_KEY = os.environ.get("FUSED_API_KEY")` yields the bare run
# `os.environ.get`, and a check that fails an app for reading its key from
# the environment is worse than no check at all. Brackets, braces, parens
# and `$` are out of the value's character set for the same reason: they
# belong to call and subscript syntax, never to a credential.
_ASSIGNMENT_SECRET_RE = re.compile(
    rb"(?i)\b([A-Z0-9_]*(?:SECRET|API[_-]?KEY|ACCESS[_-]?KEY|TOKEN|PASSWORD"
    rb"|PASSWD|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*"
    rb"(?:[\"']([^\"'\r\n]{8,})[\"']"
    rb"|([^\s\"'#\r\n()\[\]{}$,;]{8,})(?=[ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)))"
)

# A bare value that reads as a dotted name — `form.cleaned_data`,
# `settings.auth.token` — is a reference to a secret, not a secret. Real
# credentials do not arrive shaped like an attribute chain, and an app that
# passes one around by name has not leaked anything.
_DOTTED_NAME_RE = re.compile(rb"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")

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


def _is_filler_word(word: str) -> bool:
    """True for a word drawn straight from `_PLACEHOLDER_WORDS`, or for one
    that is nothing but ONE such word repeated with no separator —
    `"redactedredactedredacted"`, the shape a masked value takes once it is
    pasted somewhere that strips punctuation. `_WORD_RE` never splits a run
    like that into its repeats (there is no non-alnum character between
    them), so without this a provider key whose whole value is a placeholder
    word tripled reads as one unrecognised word and the value is judged
    real."""
    if word in _PLACEHOLDER_WORDS:
        return True
    for filler in _PLACEHOLDER_WORDS:
        if len(filler) >= 3 and word and len(word) % len(filler) == 0 \
                and word == filler * (len(word) // len(filler)):
            return True
    return False


def _is_placeholder(value: bytes) -> bool:
    stripped = value.strip()
    if _PLACEHOLDER_SYMBOL_RE.match(stripped):
        return True
    words = [w for w in _WORD_RE.split(stripped.decode("utf-8", "replace").lower()) if w]
    return bool(words) and all(_is_filler_word(w) for w in words)


# The literal, non-variable head of each `_PREFIXED_SECRET_PATTERNS` match —
# stripped off before a placeholder check, so a real prefix like `sk-ant-`
# never counts as a "word" that has to be filler too. Without this,
# `sk-ant-REDACTEDREDACTEDREDACTED` fails `_is_placeholder` outright: split on
# hyphens its first two words are "sk" and "ant", neither of which is filler,
# even though the SKILL calls this exact shape a placeholder.
_PREFIXED_SECRET_LITERAL_PREFIX = {
    "aws-access-key": re.compile(rb"^AKIA"),
    "github-token": re.compile(rb"^gh[pousr]_"),
    "slack-token": re.compile(rb"^xox[baprs]-"),
    "anthropic-key": re.compile(rb"^sk-ant-"),
    "openai-key": re.compile(rb"^sk-"),
    "google-api-key": re.compile(rb"^AIza"),
    "stripe-key": re.compile(rb"^sk_live_"),
}


def _value_is_placeholder(value: bytes) -> bool:
    """`_is_placeholder`, but also tried after stripping a known provider
    prefix off the front. `ANTHROPIC_API_KEY = "sk-ant-REDACTED..."` matches
    BOTH the anthropic-key pattern and the generic assignment pattern (the
    name reads as credential-shaped either way) — this is the one placeholder
    check both branches call, so a value judged a placeholder for one never
    turns up flagged by the other."""
    if _is_placeholder(value):
        return True
    for prefix_re in _PREFIXED_SECRET_LITERAL_PREFIX.values():
        if prefix_re.match(value):
            return _is_placeholder(prefix_re.sub(b"", value, count=1))
    return False


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
            literal_prefix = _PREFIXED_SECRET_LITERAL_PREFIX[name]
            value = literal_prefix.sub(b"", m.group(0), count=1)
            if _is_placeholder(value):
                continue
            findings.append(_family_finding(
                "secrets", f"secrets:{name}", rel_path, line_for(m.start()),
                _mask(m.group(0)),
            ))

    for m in _PRIVATE_KEY_RE.finditer(data):
        if _is_placeholder(m.group(1)):
            continue
        findings.append(_family_finding(
            "secrets", "secrets:private-key", rel_path, line_for(m.start()),
            _mask(m.group(0)),
        ))

    for m in _ASSIGNMENT_SECRET_RE.finditer(data):
        bare = m.group(2) is None
        value = m.group(2) if not bare else m.group(3)
        if _value_is_placeholder(value):
            continue
        if bare and _DOTTED_NAME_RE.match(value.strip()):
            continue
        findings.append(_family_finding(
            "secrets", "secrets:assignment", rel_path, line_for(m.start()),
            f"{m.group(1).decode()} = {_mask(value)}",
        ))


# ------------------------------------------------------------ device paths

# Absolute paths that are true statements about ONE machine, split into two
# tiers by how reliably they say so.
#
# STRONG roots are personal outright: a home directory (whoever's — the app
# is meant to move between machines and users) or a mount point (whichever
# machine mounted it, under whatever name). A path under one of these always
# fires.
#
# WEAK roots are shared OS directories that hold both personal AND purely
# system content — `/var/log`, `/tmp` used as a generic scratch dir,
# `/private/var/vm` on macOS. Firing on the bare root alone produces exactly
# the false positives measured against a real workspace: `/private/var/vm`
# inside a deliberate `SKIP_DIRS` constant, and `/media/cover.png`, a
# relative URL sitting in test HTML. So a weak root only fires when the path
# continues PAST a bare OS-jargon directory name into something that reads as
# user- or project-specific — see `_is_weak_device_root_false_positive`.
_STRONG_DEVICE_ROOTS = ("/home/", "/Users/", "/root/", "/Volumes/")
_WEAK_DEVICE_ROOTS = ("/opt/", "/var/", "/tmp/", "/mnt/", "/media/", "/private/")
_DEVICE_ROOTS = _STRONG_DEVICE_ROOTS + _WEAK_DEVICE_ROOTS

# OS-jargon directory names that show up right after a weak root and mean
# "this machine's own plumbing" — the same vocabulary every *nix ships,
# regardless of who is logged in, never a stand-in for someone's own data.
_WEAK_ROOT_SYSTEM_SEGMENTS = frozenset({
    "var", "tmp", "etc", "opt", "log", "logs", "run", "lib", "cache",
    "spool", "bin", "sbin", "proc", "sys", "dev", "mail", "folders",
    "db", "vm", "empty", "root",
})

# Prose has no runtime behaviour to break: a path merely mentioned in
# documentation is not a path an app depends on, and every measured
# markdown false positive turned out to live inside a fenced code span,
# where a backtick used to read as an opening quote (see `_quote_precedes`).
# Skipping the whole family for these suffixes is simpler and more correct
# than trying to tell a real fenced span from a stray backtick.
_PROSE_SUFFIXES = (".md", ".rst", ".txt")

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
    an HTML attribute) — as opposed to a bare word inside a sentence of
    prose, which this engine leaves alone. A backtick does NOT count: it
    opens a markdown code span, which is prose showing an example, not
    source with a real assignment — the whole prose-file family is skipped
    anyway (`_PROSE_SUFFIXES`), but a backtick can just as easily wrap a path
    inside a docstring or a comment in a real source file."""
    return match_start > 0 and text[match_start - 1] in "\"'"


def _is_weak_device_root_false_positive(match_text: str) -> bool:
    """True when `match_text` (the whole match, root included) sits under a
    WEAK root but does not continue far enough past a bare OS directory name
    to say anything about a real person's data. A match with fewer than two
    segments past the root (`/media/cover.png`) is a root plus one generic
    name, not a path into someone's own tree; a match whose first segment
    past the root is itself OS jargon (`/private/var/vm`) is still talking
    about the machine, not a person, however many segments follow."""
    root = next((r for r in _WEAK_DEVICE_ROOTS if match_text.startswith(r)), None)
    if root is None:
        return False
    segments = match_text[len(root):].split("/")
    if len(segments) < 2:
        return True
    return segments[0].lower() in _WEAK_ROOT_SYSTEM_SEGMENTS


def _check_device_paths(rel_path: str, text: str, findings: list) -> None:
    if rel_path.lower().endswith(_PROSE_SUFFIXES):
        return
    for m in _POSIX_DEVICE_RE.finditer(text):
        if _preceded_by_url_host(text, m.start()) or not _quote_precedes(text, m.start()):
            continue
        if _is_weak_device_root_false_positive(m.group(0)):
            continue
        findings.append(_family_finding(
            "device-paths", "device-path:hardcoded", rel_path, _line_of(text, m.start()),
            m.group(0),
        ))
    for m in _WIN_DEVICE_RE.finditer(text):
        if not _quote_precedes(text, m.start()):
            continue
        findings.append(_family_finding(
            "device-paths", "device-path:hardcoded", rel_path, _line_of(text, m.start()),
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
        findings.append(_structure_finding(
            "structure:missing-index",
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
        findings.append(_structure_finding(
            "structure:missing-readme",
            "no README in the app folder — say what this app does for whoever you share it with",
        ))

    try:
        has_preview = (_PREVIEW_IMAGE_NAME in os.listdir(app_dir)
                       and os.path.getsize(
                           os.path.join(app_dir, _PREVIEW_IMAGE_NAME)) > 0)
    except OSError:
        has_preview = False
    if not has_preview:
        findings.append(_structure_finding(
            "structure:missing-thumbnail",
            f"no {_PREVIEW_IMAGE_NAME} thumbnail (or it is empty) — this is how "
            "the app is recognized in a grid of others",
        ))


# --------------------------------------------------------------------- API


def check(app_dir: str) -> list[dict]:
    """Every finding for `app_dir`: `{rule, section, severity, kind, path,
    line, excerpt}`, `path` always relative to `app_dir`. `kind` is `"fact"`
    for the three structure gaps and `"candidate"` for the two pattern-match
    families (secrets, device paths) — see the module docstring for why that
    split exists and what it changes about `main`'s exit code. Never
    raises — an unreadable app is one the caller already knows is broken some
    other way, and a doctor that crashes on the app it was asked to examine
    is not useful to anyone.

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
    """Review one app folder and print one line per finding.

    Each line is `path:line: rule: excerpt` — the `path:line:` prefix is the
    shape an editor or `grep` already understands, so a line pastes straight
    into a jump-to-location, and the full `rule` (not just its family) is
    what a person needs to look up or argue with. A finding with no line
    number (the structure family reports against the app folder itself)
    drops the line and its colon: `path: rule: excerpt`. Findings are sorted
    by path, then line, then rule, so the same app always prints the same
    bytes in the same order. Every finding prints, candidate and fact alike —
    only the exit code tells them apart.

    Fails on a FACT finding, never on a candidate alone: see the module
    docstring for why (a real workspace measurement where every candidate
    finding was a false positive). Returns the process exit code rather than
    calling `sys.exit` itself, so a caller in the same process can inspect
    it."""
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

    for f in sorted(findings, key=lambda f: (f["path"], f["line"], f["rule"])):
        where = f["path"] if not f["line"] else f"{f['path']}:{f['line']}"
        print(f"{where}: {f['rule']}: {f['excerpt']}")
    print(f"{len(findings)} finding" + ("" if len(findings) == 1 else "s"))
    return 1 if any(f.get("kind") == "fact" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
