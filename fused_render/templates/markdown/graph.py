"""Link parsing, resolution and backlinks for the markdown template (SPEC §32).

`main()` is the template's data side: it answers "what does this note link to,
and what links back". Everything the note view, the local graph panel and the
folder-level graph mode draw comes from the same three functions here, so the
three surfaces can never disagree about what a link is (MD-3) or where it
points (MD-4).

Three rules shape the whole module:

1. **Links are parsed from the source with code elided.** `_mask_code` blanks
   fenced blocks, indented blocks, inline spans and the YAML frontmatter to
   spaces of the same length, so a `[[Note]]` in a code sample is not an edge
   and byte offsets still line up with the original text.

2. **Resolution happens at assembly time, never at parse/index time** (MD-6).
   `parse_note` records the *raw* target exactly as authored; `resolve_link`
   turns it into a path against the candidate set that exists right now.
   Renaming `Foo.md` silently changes what every other note's `[[Foo]]` points
   at, so a cached resolved edge would be wrong rather than merely stale.

3. **The recursive walk never touches a remote mount** (MD-11). `scan_root`
   raises `MountUnsupported` for a mount-backed root before it walks anything —
   a kernel listing over an rclone NFS mount on a flat million-key prefix is
   the known mount-wedge, and the folder mode's `condition.py` refuses the same
   paths so the mode is never even offered. Belt and braces: the gate is the
   UX, this is the guarantee. Single-file read/write on a mount stays fully
   supported — that is one bounded read, not a walk.
"""
import os
import re
import sys

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "graph.py")

_HERE = os.path.dirname(os.path.abspath(__file__))
# `../shared/appenv.py` is how this template asks the app about its environment
# (SPEC PY-14): env vars only, stdlib only, no `fused_render` import. The import
# stays LAZY at each use site below so an unreachable appenv is still the
# "cannot tell" case MD-11 turns into a refusal, rather than a module that
# fails to load at all.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "shared"))

# Bumped whenever parse_note's output shape or semantics change, so a stored
# index row from an older parser is invalidated wholesale (MD-8).
#
# 2: `title` is gone from the row — a note's display name is its filename now,
# so a frontmatter `title:` or a leading `# H1` no longer renames it.
# 3: `tags` is gone from the row — the tag concept was removed (D165), so a
# cached row still carrying it would feed tag nodes to a graph that has none.
PARSER_VERSION = 3

# What counts as a note. Both are bound to this template in registry.json.
NOTE_SUFFIXES = (".md", ".markdown")

# There is deliberately NO per-note size cap. One used to skip any `.md` over
# 256 KB, on the theory that such a file is a generated changelog rather than a
# note and that reading it would dominate the walk. Both halves were wrong in
# practice: a long-lived decision log or design doc is exactly a note someone
# wants backlinks into, and a skipped file landed in NEITHER `notes` nor
# `assets`, so every `[[…]]` aimed at it resolved to nothing and drew a ghost —
# indistinguishable from a link to a note that does not exist. The cost the cap
# was avoiding is also already paid once and cached: the index keys parses on
# `(mtime_ns, size)`, so a big note is read when it changes and stat-only on
# every open after that. The walk's real budget is MAX_ENTRIES below, which
# bounds the number of files rather than the size of any one of them.
#
# The editor's own inline-edit ceiling (`MAX_BYTES` in template.html) is a
# separate 2 MB guard and was never this cap — a 500 KB note has always loaded
# and edited fine, it just had no place in the graph.

# Hard caps on what a single walk RECORDS. Exceeding either is reported so the
# UI can say the graph is partial rather than quietly showing a subset (MD-10).
MAX_FILES = 5000
MAX_ASSETS = 5000

# Hard cap on what a single walk ENUMERATES — every directory entry it visits,
# note or not. The two caps above bound only the lists that come back, which is
# not the same thing: a tree of 20k generated files beside one note used to be
# `readdir`'d in full for nothing, and this walk runs on EVERY .md open (a warm
# open is stat-only, so the walk is the steady-state cost of opening a note).
# 20k is ~15x the largest real tree measured (openfused visits ~1.4k entries,
# this repo ~0.6k), so no vault or docs repo trips it, and it holds the
# pathological case to tens of milliseconds instead of the size of a monorepo.
MAX_ENTRIES = 20000

# Directories a note vault never means to include: dotdirs (.git, .obsidian,
# .venv, .tox, .next) plus the usual vendored and build-output trees, whose
# markdown is generated or someone else's. Skipping them is two wins at once —
# a vendored README stops being drawn as a graph node the author appears to have
# written, and the walk stops paying for the subtree at all.
#
# `.gitignore` is deliberately NOT consulted, and that is a decision rather than
# an oversight: pattern matching with negations, `**` and nested ignore files is
# real complexity, and it silently does nothing outside a git repo — which is
# exactly where a vault often lives. Obsidian likewise keeps its own exclusion
# list. `server._walk_bfs` can afford git's own answer (a `check-ignore`
# co-process per repo, D100); this walk runs on every note open and cannot. A
# fixed name list is what we can reason about, so it stays the whole mechanism.
#
# The list is deliberately conservative: a skipped directory is invisible with no
# notice at all, so a folder of real notes silently missing is a worse failure
# than a generated one showing up. Every name here is one that only a tool
# writes — `docs`, `site`, `public`, `static`, `content`, `notes`, `output` and
# `bin` were all considered and rejected as names a person plausibly keeps their
# own writing (or a hand-built site's sources) in.
SKIP_DIRS = frozenset((
    # dependencies vendored into the tree
    "node_modules", "bower_components", "site-packages", "venv", "vendor", "Pods",
    # compiler / bundler / doc-generator output
    "__pycache__", "build", "_build", "dist", "out", "target", "_site",
    # coverage reports
    "coverage", "htmlcov",
))


class MountUnsupported(Exception):
    """Raised instead of walking a mount-backed root (MD-11)."""


# --------------------------------------------------------------- mount refusal


def _refuse_mounts(root: str) -> None:
    """Refuse a mount-backed root outright.

    The detector is `shared/appenv.is_mount_backed`, a faithful port of the
    app's `shell.mounts.is_mount_backed` that answers from `FUSED_RENDER_*`
    instead of importing `fused_render`. It has to: this file runs as a child
    process, and the fused local execution backend strips PYTHONPATH from those,
    so the old `from fused_render.shell.mounts import ...` took its except branch
    on EVERY run there and refused every root. An ImportError still means we
    cannot tell, and "cannot tell" must read as "refuse": a walk we were not
    allowed to do is the failure this exists to prevent.
    """
    try:
        from appenv import is_mount_backed
    except Exception as exc:  # noqa: BLE001 — cannot tell -> refuse
        raise MountUnsupported(f"mount detection unavailable: {exc}") from exc
    if is_mount_backed(root):
        raise MountUnsupported(
            "The link graph is not supported on remote mounts. "
            "Opening and editing a single .md file still works.")


# ----------------------------------------------------------------- vault root

# What marks the top of a vault. `.obsidian/` is Obsidian's own marker; `.git`
# is a DIRECTORY in a clone and a FILE in a worktree, so both shapes are probed.
# A third marker, `.fused-graph.json`, was dropped as never adopted (D165) —
# these two cover every vault anyone actually keeps.
VAULT_MARKERS = (".obsidian", ".git")

# How far up the ascent may look. Deep enough for any real vault layout, and
# shallow enough that a note in a deep temp path cannot drag the scan up to
# $HOME by accident.
MAX_ASCENT = 8


def _mount_detector():
    """`is_mount_backed`, or None when we cannot tell (MD-11's fail-closed rule)."""
    try:
        from appenv import is_mount_backed
    except Exception:  # noqa: BLE001 — cannot tell -> do not ascend
        return None
    return is_mount_backed


def _has_vault_marker(directory: str) -> bool:
    """Whether `directory` carries one of VAULT_MARKERS.

    A fixed set of `isdir`/`isfile` probes and NEVER a listing — the discipline
    `graph/condition.py` documents (CT-12). A probe is constant-time however
    many entries the level holds; a listing is proportional to them, and this
    runs per level of the climb on every note the user opens.
    """
    for name in VAULT_MARKERS:
        candidate = os.path.join(directory, name)
        if os.path.isdir(candidate) or os.path.isfile(candidate):
            return True
    return False


def vault_root(start: str) -> str:
    """The nearest ancestor of `start` that looks like a vault root, else `start`.

    The note's own folder used to be the default scan root, and it was too
    narrow to be useful (MD-12): every link leaving the folder rendered as a
    ghost and every inbound link from outside it was invisible, so a note in
    `v/docs/` linking `../spec/overview.md` got a halo of grey `../…` ghosts and
    an empty backlinks panel.

    Bounded (MAX_ASCENT levels, and the filesystem root ends it either way) and
    mount-aware: the climb never enters a mount-backed path, because a walk over
    one is the thing MD-11 exists to prevent — and because a local note that
    merely lives under a mounted folder should be scanned in the folder it is
    actually in, not answered with `mount_unsupported`. Falls back to `start`
    when no marker is found: never `$HOME`, never `/`.
    """
    start = os.path.abspath(start)
    detect = _mount_detector()
    if detect is None:
        return start
    try:
        if detect(start):
            return start  # _refuse_mounts has the last word on this one anyway
    except Exception:  # noqa: BLE001 — cannot tell -> do not ascend
        return start
    current = start
    for _ in range(MAX_ASCENT + 1):
        if _has_vault_marker(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break  # the filesystem root
        try:
            if detect(parent):
                break
        except Exception:  # noqa: BLE001 — cannot tell -> stop climbing
            break
        current = parent
    return start


def _default_root(file: str) -> str:
    """The scan root for `file` when no explicit `root` param was given."""
    own = os.path.dirname(os.path.abspath(file))
    root = vault_root(own)
    # Every candidate the ascent can return is an ancestor of the note's own
    # folder, so this holds by construction — checked rather than assumed,
    # because a root that did not contain the file would turn the note view into
    # an `outside_root` error for every note in the vault.
    if own != root and not own.startswith(root.rstrip(os.sep) + os.sep):
        return own
    return root


def _coerce_depth(depth, fallback: int = 1) -> int:
    """`depth` reaches `main` as a STRING from the template (`String(depth)`),
    and `_neighbourhood` compares it with an int — `range(max(0, "2"))` raises.
    Coerced at the public entry point rather than trusting every caller, and a
    nonsense value falls back instead of throwing: this is a URL param.

    A NEGATIVE depth is meaningful, not garbage — it is the panel's `all`
    sentinel (see `_graph_payload`) — so the coercion must pass it through."""
    try:
        return int(depth)
    except (TypeError, ValueError):
        return fallback


# ------------------------------------------------------------------- parsing

# A fence opener/closer: up to 3 leading spaces, then 3+ backticks or tildes.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# Inline code spans: a backtick run, the shortest span closing on an equal run.
_INLINE_CODE = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)")
# `[[Target#Heading|label]]`, `![[…]]` for an embed. No nesting, one line.
_WIKILINK = re.compile(r"(?P<bang>!?)\[\[(?P<inner>[^\[\]\n]+?)\]\]")
# `[label](target)` / `![alt](target)` with an optional "title".
_MDLINK = re.compile(
    r"(?P<bang>!?)\[(?P<label>[^\]\n]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"\n]*\")?\)")
_HEADING = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$")

# A markdown-link target that is not a path into this vault.
_NOT_A_PATH = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", re.I)


def _blank(text: str) -> str:
    """Same length, same line breaks, no content — so masked regions keep every
    offset and line number the original had."""
    return "".join("\n" if ch == "\n" else " " for ch in text)


def _frontmatter_span(text: str):
    """`(start, end)` of a leading YAML frontmatter block, or None.

    Only a document whose very first line is `---` has frontmatter; a `---`
    anywhere else is a horizontal rule.
    """
    if not text.startswith("---"):
        return None
    first_break = text.find("\n")
    if first_break == -1 or text[:first_break].strip() != "---":
        return None
    for match in re.finditer(r"^(?:---|\.\.\.)[ \t]*$", text, re.M):
        if match.start() > first_break:
            return (0, match.end())
    return None


def _mask_code(text: str, frontmatter):
    """`text` with frontmatter and every code region replaced by blanks."""
    if frontmatter is not None:
        start, end = frontmatter
        text = _blank(text[start:end]) + text[end:]

    lines = text.split("\n")
    fence = None  # the opening run while inside a fenced block
    previous_blank = True
    for i, line in enumerate(lines):
        opener = _FENCE.match(line)
        if fence is not None:
            # Inside a fence: everything is code, including a closing line.
            lines[i] = _blank(line)
            if opener is not None and opener.group(1)[0] == fence[0] and \
                    len(opener.group(1)) >= len(fence):
                fence = None
            continue
        if opener is not None:
            fence = opener.group(1)
            lines[i] = _blank(line)
            continue
        # An indented code block only starts after a blank line; otherwise four
        # spaces are a wrapped list item, not code.
        if previous_blank and (line.startswith("    ") or line.startswith("\t")) \
                and line.strip():
            lines[i] = _blank(line)
            continue
        previous_blank = not line.strip()
        lines[i] = _INLINE_CODE.sub(lambda m: _blank(m.group(0)), line)
    return "\n".join(lines)


def _wikilink(match) -> dict:
    inner = match.group("inner")
    target, _, label = inner.partition("|")
    target, _, heading = target.partition("#")
    return {
        "target": target.strip(),
        "heading": heading.strip() or None,
        "label": label.strip() or None,
        "embed": bool(match.group("bang")),
        "wiki": True,
        "offset": match.start(),
    }


def _mdlink(match):
    target = match.group("target").strip()
    if not target or _NOT_A_PATH.match(target):
        return None
    path, _, heading = target.partition("#")
    if not path:
        return None
    return {
        "target": path,
        "heading": heading or None,
        "label": match.group("label").strip() or None,
        "embed": bool(match.group("bang")),
        "wiki": False,
        "offset": match.start(),
    }


def parse_note(text: str) -> dict:
    """Everything one note contributes to the graph, from its source alone.

    Returns `{"headings", "links"}`. `links` carries the RAW authored target
    (MD-6) in document order.

    No `title`: a note is named by its FILE, which parse_note cannot see and
    deliberately does not try to override. A frontmatter `title:` and a leading
    `# H1` used to win over the filename, which meant the name on a graph node
    and in a backlink row was not the name you would search for, rename, or type
    inside `[[…]]` — and an `# H1` that merely repeated the filename made the
    two agree often enough to hide the cases where they did not. Obsidian names
    a note by its file for the same reason. Headings are still parsed in full;
    they are just headings now.
    """
    frontmatter = _frontmatter_span(text)
    body = _mask_code(text, frontmatter)

    links = [_wikilink(m) for m in _WIKILINK.finditer(body)]
    # A wikilink is not an md-link, but `![[x]]`'s trailing `]]` cannot be
    # mistaken for one either — so the two scans are independent and merge by
    # position. Drop md-link matches that overlap a wikilink for safety.
    wiki_spans = [(m.start(), m.end()) for m in _WIKILINK.finditer(body)]
    for match in _MDLINK.finditer(body):
        if any(start <= match.start() < end for start, end in wiki_spans):
            continue
        link = _mdlink(match)
        if link is not None:
            links.append(link)
    links.sort(key=lambda link: link["offset"])
    for link in links:
        link.pop("offset")

    headings = []
    for line in body.split("\n"):
        match = _HEADING.match(line)
        if match is not None:
            headings.append({"level": len(match.group("hashes")),
                             "text": match.group("text").strip()})

    return {
        "headings": headings,
        "links": links,
    }


# ---------------------------------------------------------------- resolution


def _stem(rel: str) -> str:
    lower = rel.lower()
    for suffix in NOTE_SUFFIXES:
        if lower.endswith(suffix):
            return rel[: -len(suffix)]
    return rel


def _client_path(path: str) -> str:
    """An absolute path in the form the PAGE speaks: forward slashes throughout.

    Everything above this line does filesystem work and rightly uses native
    separators — `os.path.abspath` hands back `C:\\Users\\me\\vault` on Windows.
    But every path that crosses into a payload is then split and re-joined on
    "/" by the template, and the shell's own canonical form for a Windows file
    is the drive path `C:/Users/…`. A native separator therefore survives the
    trip only to be mis-split at the other end, so it is converted exactly once,
    here, at the boundary.
    """
    return path.replace("\\", "/") if path else path


def _client_join(root: str, rel: str) -> str:
    """`root` + a vault-relative `rel`, as one client-facing absolute path.

    Pure string work on purpose: `os.path.join` would reintroduce the native
    separator this exists to remove, and `rel` is already POSIX by construction.
    """
    base = _client_path(root).rstrip("/")
    return base + "/" + rel if rel else base


def _normalize_target(target: str) -> str:
    target = (target or "").strip().replace("\\", "/")
    while target.startswith("./"):
        target = target[2:]
    return target.strip("/")


# A suffix that makes a target a FILE of some other kind: a dot, then a letter,
# then up to four more word characters. The leading letter is what keeps
# `[[Chapter 1.2]]` and `[[v1.0]]` linkable — a version is not an extension.
_OTHER_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,4}$")


def _ghostable(target: str) -> bool:
    """Whether an unresolved target could ever *be* a note (MD-4).

    A ghost is a promise — click it and that note appears — so a target that can
    never name a note must not become one. Two cases:

    * a **directory**: `[../examples/](../examples/)` is a link to a folder, and
      the ghost it used to make was labelled `../examples/`, from which the
      template derived a file literally called `.md`, one level above the vault
      root (the reported bug);
    * a **file of another kind**: `../scripts/run.py` either exists or does not,
      but it is never a note, and offering to create `run.py.md` is a lie. (An
      *embed* of a non-note resolves through the asset index instead, so this
      only ever fires on targets nothing could have resolved.)

    Judged on the target STRING alone: no stat and no listing, because this runs
    once per link per note — the same discipline as the vault-root ascent.
    """
    raw = (target or "").strip().replace("\\", "/")
    if not raw or raw.endswith("/") or not _normalize_target(raw):
        return False
    base = raw.rsplit("/", 1)[-1]
    return _is_note(base) or _OTHER_EXT_RE.search(base) is None


def _posix_join(base: str, rel: str) -> str:
    """Join two vault-relative POSIX paths, collapsing `.`/`..`. Pure string
    work — never touches the filesystem, so it is safe on any root."""
    out = [p for p in base.split("/") if p and p != "."]
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if out:
                out.pop()
        else:
            out.append(part)
    return "/".join(out)


def _candidate_index(paths):
    """`{lowercased key: [relpath, …]}` — each candidate under both its full
    relative path and its extension-stripped stem, so `[[docs/Note]]` and
    `[[docs/Note.md]]` are the same link."""
    index = {}
    for rel in paths:
        for key in {rel.lower(), _stem(rel).lower()}:
            index.setdefault(key, []).append(rel)
    return index


def _only(hits):
    """The single match, or None when a link is ambiguous.

    Ambiguity is deliberately a ghost rather than a guess (MD-4): two notes
    sharing a basename means the link did not carry enough path to say which,
    and silently picking one makes the graph assert an edge the author never
    wrote.
    """
    unique = sorted(set(hits or ()))
    return unique[0] if len(unique) == 1 else None


def resolve_link(target: str, from_rel: str, paths, index=None):
    """Resolve a raw link target to a vault-relative path, or None.

    **Shortest path that is unambiguous** (MD-4), tried in order: relative to
    the linking note's own folder, then from the vault root, then as a path
    suffix. Case-insensitive, as Obsidian is. `paths` is the candidate set that
    exists *now*; `index` is the memoised `_candidate_index` of it when a caller
    resolves many links at once.

    Three steps, not four. A fourth ("then as a bare basename") was documented
    and written but could never run: it sat behind an `if "/" in lowered: return
    None`, so the target had no slash by then and splitting one off its last
    slash gave the target back — a repeat of the root lookup that had already
    failed, so the answer was always None. It
    was redundant as well as dead: a bare basename is found by the root lookup
    when the note is top-level and by the suffix step when it is not. Deleted
    rather than made live, because making it live would CHANGE resolution
    (turning currently-unresolved links into resolved ones) and MD-4's rule is
    the shortest path that is UNAMBIGUOUS.
    """
    normalized = _normalize_target(target)
    if not normalized:
        return None
    index = _candidate_index(paths) if index is None else index
    lowered = normalized.lower()

    here = from_rel.rsplit("/", 1)[0] if "/" in (from_rel or "") else ""
    for key in (_posix_join(here, normalized).lower(), lowered):
        hit = _only(index.get(key))
        if hit is not None:
            return hit

    suffix = "/" + lowered
    return _only([rel for key, rels in index.items() if key.endswith(suffix)
                  for rel in rels])


# --------------------------------------------------------------------- walking


def _is_note(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in NOTE_SUFFIXES)


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIRS


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _walk(root: str, max_files: int, max_assets: int,
          max_entries: int = MAX_ENTRIES) -> dict:
    """The walk's facts, with nothing parsed: which notes exist and how big/old
    each one is. Split from parsing so the index can decide, per file, whether a
    read is needed at all (MD-8) — a warm walk is stat-only.

    Deterministic: directories and files are visited in sorted order, so a cap
    that fires drops the same tail every time rather than an arbitrary subset.
    That is also why `max_entries` is checked per entry but the tail is only
    abandoned at a directory boundary: sorting a directory needs its whole
    listing, so the listing is read either way — what the budget stops is every
    directory BELOW the point it ran out, which is where the cost lives.

    Every cap that fires sets `truncated`, including the asset cap: a walk that
    kept nothing while still enumerating is the exact failure MD-10 forbids,
    since both the note view and the folder graph render their "partial" notice
    off this flag alone. Only the caps that make further walking pointless stop
    it, though — a full asset list is reported and walked past, because notes are
    the payload and dropping them to save asset slots would be the worse trade.
    """
    found = {}      # rel -> (abs path, mtime_ns, size)
    assets = []
    truncated = False
    stop = False    # a cap that makes the rest of the tree pointless to visit
    entries = 0     # every directory entry visited, note or not

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
        entries += len(dirnames)
        # The budget is tested HERE as well as per file, and the second test is
        # the load-bearing one: a subtree of directories holding no files never
        # enters the loop below, so with the test only there a directory-only
        # tree spent the whole budget and kept walking — 30k empty directories
        # listed in full, reporting `truncated=False`. Each directory costs a
        # listing whether or not it holds a file, which is what the budget is
        # counting.
        if entries > max_entries:
            truncated = stop = True
            break
        for name in sorted(filenames):
            entries += 1
            if entries > max_entries:
                truncated = stop = True
                break
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not _is_note(name):
                if len(assets) < max_assets:
                    assets.append(rel)
                else:
                    truncated = True
                continue
            if len(found) >= max_files:
                truncated = stop = True
                break
            try:
                # Still stat'd, but only for the index's cache key — nothing
                # here judges a note by its size any more.
                stat = os.stat(full)
            except OSError:
                continue
            found[rel] = (full, stat.st_mtime_ns, stat.st_size)
        if stop:
            break

    return {"found": found, "assets": assets, "truncated": truncated}


def _row(full: str, mtime_ns: int, size: int):
    """Parse one note into an index row, or None when it cannot be read."""
    try:
        text = _read_text(full)
    except OSError:
        return None
    row = parse_note(text)
    row["mtime_ns"] = mtime_ns
    row["size"] = size
    return row


def _assemble(root: str, walk: dict, notes: dict) -> dict:
    return {
        "root": root,
        "notes": notes,
        "assets": walk["assets"],
        "truncated": walk["truncated"],
    }


def scan_root(root: str, max_files: int = MAX_FILES,
              max_assets: int = MAX_ASSETS, max_entries: int = MAX_ENTRIES) -> dict:
    """Walk `root` and parse every note under it, with no cache involved.

    Returns `{"root", "notes": {relpath: row}, "assets": [relpath],
    "truncated"}`. Refuses a mount-backed root before walking (MD-11).
    """
    root = os.path.abspath(root)
    _refuse_mounts(root)
    walk = _walk(root, max_files, max_assets, max_entries)
    notes = {}
    for rel, (full, mtime_ns, size) in walk["found"].items():
        row = _row(full, mtime_ns, size)
        if row is not None:
            notes[rel] = row
    return _assemble(root, walk, notes)


# ----------------------------------------------------------------- the index
#
# Tier 1 of the three-tier story (MD-8): per-file parses are cached on disk,
# the graph itself never is. A row is invalid when `(mtime_ns, size)` differs
# from disk, or when `parser_version` moved — which invalidates everything at
# once, so changing parse_note needs no migration.
#
# The index is a CACHE. Every failure mode — no sqlite, a corrupt file, a
# read-only home — costs a full walk and nothing else; none of them may turn
# into an error the user sees.

SCHEMA_VERSION = 1

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS notes ("
    " rel TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL,"
    " size INTEGER NOT NULL, data TEXT NOT NULL)",
)


def index_dir() -> str:
    """`~/.fused-render/graph`, resolved against `appenv.home_dir()` each call so
    FUSED_RENDER_HOME overrides (and the per-branch nesting) work — the
    established pattern from core_templates.CORE_TEMPLATES_DIR.

    Per call, not cached: `appenv` reads the env every time, and this module can
    be long-lived. The value arrives ALREADY branch-resolved from the server, so
    nothing here re-derives the nesting."""
    from appenv import home_dir

    return os.path.join(home_dir(), "graph")


def index_path(root: str) -> str:
    """The index file for `root`: `<index_dir>/<sha256 of realpath>.sqlite`.

    Keyed on realpath so a symlink and its target share one index, and never an
    in-folder sidecar — no repo pollution, nothing to gitignore. The absolute
    root is also stored INSIDE the db (see `index_meta`), so a moved folder or a
    hash collision is detectable rather than silently mis-attributed.
    """
    import hashlib

    real = os.path.realpath(root)
    digest = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()
    return os.path.join(index_dir(), digest + ".sqlite")


def _connect(root: str):
    """An open, schema-current connection whose rows belong to `root`, or None.

    Any unusable db is discarded and rebuilt once; a second failure gives up and
    returns None so the caller does a plain walk.
    """
    import sqlite3

    path = index_path(root)
    real = os.path.realpath(root)
    for attempt in (0, 1):
        # Bound BEFORE the try: `os.makedirs` and `sqlite3.connect` can both
        # fail (an unwritable home, a directory where the db should be), and the
        # except branch below closes `conn` — an unbound name there would turn a
        # cache miss into a NameError escaping into the caller's run.
        conn = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            conn = sqlite3.connect(path)
            for statement in _SCHEMA:
                conn.execute(statement)
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            stale = (
                meta.get("root") != real
                or meta.get("parser_version") != str(PARSER_VERSION)
                or meta.get("schema_version") != str(SCHEMA_VERSION)
            )
            if stale:
                conn.execute("DELETE FROM notes")
                conn.executemany(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (("root", real), ("parser_version", str(PARSER_VERSION)),
                     ("schema_version", str(SCHEMA_VERSION))))
                conn.commit()
            return conn
        except Exception:  # noqa: BLE001 — a cache failure is never fatal
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            if attempt == 0:
                try:
                    os.remove(path)
                except OSError:
                    return None
            else:
                return None
    return None


def index_meta(root: str) -> dict:
    """The index's own metadata (`root`, `parser_version`, `schema_version`)."""
    conn = _connect(root)
    if conn is None:
        return {}
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def index_rows(root: str) -> dict:
    """`{rel: (mtime_ns, size)}` currently cached for `root`."""
    conn = _connect(root)
    if conn is None:
        return {}
    try:
        return {rel: (mtime_ns, size) for rel, mtime_ns, size
                in conn.execute("SELECT rel, mtime_ns, size FROM notes")}
    finally:
        conn.close()


def scan_indexed(root: str, max_files: int = MAX_FILES,
                 max_assets: int = MAX_ASSETS, max_entries: int = MAX_ENTRIES) -> dict:
    """`scan_root`, but reading unchanged notes out of the on-disk index.

    Cold: one walk plus N reads. Warm: one stat-only walk plus reads for changed
    files, typically zero. Deletions are free — the assembly only uses rows the
    current walk found — and the rows for vanished files are dropped so the db
    cannot grow without bound.

    This is also what makes a large note affordable now that nothing is skipped
    for its size: a 300 KB decision log is read on the open after it changes and
    never again until it changes next.
    """
    import json

    root = os.path.abspath(root)
    # Before anything, including creating the index dir: the walk below is the
    # operation that must never happen on a mount (MD-11).
    _refuse_mounts(root)
    walk = _walk(root, max_files, max_assets, max_entries)
    found = walk["found"]

    conn = _connect(root)
    if conn is None:
        notes = {}
        for rel, (full, mtime_ns, size) in found.items():
            row = _row(full, mtime_ns, size)
            if row is not None:
                notes[rel] = row
        return _assemble(root, walk, notes)

    try:
        cached = {}
        for rel, mtime_ns, size, data in conn.execute(
                "SELECT rel, mtime_ns, size, data FROM notes"):
            cached[rel] = (mtime_ns, size, data)

        notes = {}
        writes = []
        for rel, (full, mtime_ns, size) in found.items():
            hit = cached.get(rel)
            if hit is not None and hit[0] == mtime_ns and hit[1] == size:
                try:
                    row = json.loads(hit[2])
                except ValueError:
                    row = None
                if isinstance(row, dict):
                    notes[rel] = row
                    continue
            row = _row(full, mtime_ns, size)
            if row is None:
                continue
            notes[rel] = row
            writes.append((rel, mtime_ns, size, json.dumps(row)))

        gone = [(rel,) for rel in cached if rel not in found]
        if writes or gone:
            conn.executemany(
                "INSERT OR REPLACE INTO notes (rel, mtime_ns, size, data)"
                " VALUES (?, ?, ?, ?)", writes)
            conn.executemany("DELETE FROM notes WHERE rel = ?", gone)
            conn.commit()
        return _assemble(root, walk, notes)
    except Exception:  # noqa: BLE001 — the cache never fails the caller
        notes = {}
        for rel, (full, mtime_ns, size) in found.items():
            row = _row(full, mtime_ns, size)
            if row is not None:
                notes[rel] = row
        return _assemble(root, walk, notes)
    finally:
        conn.close()


# --------------------------------------------------------------------- the API


def _display_title(rel: str) -> str:
    """A note's display name: its filename, without directory or extension.

    Takes only the path, so it needs no row and works for a note the scan never
    parsed — which is the whole point of naming by file (see `parse_note`).
    """
    return os.path.basename(_stem(rel))


def _resolved_links(rel: str, row, note_index, asset_index):
    """Each of a note's authored links with the path it points at right now."""
    out = []
    for link in row.get("links", []):
        if not link["target"]:
            # `[[#Heading]]` — an anchor inside this same note, not an edge.
            continue
        index = asset_index if link["embed"] and not _is_note(link["target"]) else note_index
        out.append({**link, "rel": resolve_link(link["target"], rel, (), index)})
    return out


def _note_payload(root: str, rel: str, scan: dict) -> dict:
    notes = scan["notes"]
    note_index = _candidate_index(notes)
    asset_index = _candidate_index(list(notes) + scan["assets"])
    row = notes.get(rel)
    if row is None:
        # Present on disk but not in the scan — under a skipped directory, or
        # past a cap the walk hit. No longer reachable by being large, but the
        # fallback stays: parse it directly so the note view still works.
        row = parse_note(_read_text(os.path.join(root, rel)))

    def absolute(target_rel):
        return _client_join(root, target_rel) if target_rel else None

    links = []
    for link in _resolved_links(rel, row, note_index, asset_index):
        links.append({
            "target": link["target"],
            "heading": link["heading"],
            "label": link["label"],
            "embed": link["embed"],
            "wiki": link["wiki"],
            "path": absolute(link["rel"]),
            "title": _display_title(link["rel"]) if link["rel"] else None,
        })

    backlinks = []
    for other_rel in sorted(notes):
        if other_rel == rel:
            continue
        for link in _resolved_links(other_rel, notes[other_rel], note_index, asset_index):
            if link["rel"] != rel:
                continue
            backlinks.append({
                "path": absolute(other_rel),
                "rel": other_rel,
                "title": _display_title(other_rel),
                "label": link["label"],
                "heading": link["heading"],
                "embed": link["embed"],
            })
    return {
        "error": None,
        "root": _client_path(root),
        "rel": rel,
        "title": _display_title(rel),
        "headings": row["headings"],
        "links": links,
        "backlinks": backlinks,
        "notes": len(notes),
        "truncated": scan["truncated"],
        "parser_version": PARSER_VERSION,
    }


def _link_form(rel: str, paths, index) -> str:
    """The shortest form of `rel` that still resolves to `rel` — what the `[[`
    popup inserts (MD-14).

    Obsidian's "new link format: shortest path when possible", made honest by
    construction: each candidate form is run through `resolve_link` itself, so
    the popup can never insert a link the resolver would read as a ghost or as
    somebody else's note.
    """
    segments = _stem(rel).split("/")
    for depth in range(1, len(segments)):
        form = "/".join(segments[-depth:])
        if resolve_link(form, "", paths, index) == rel:
            return form
    return "/".join(segments)


def _candidates_payload(root: str, scan: dict) -> dict:
    """Everything the `[[` and `[[note#` popups offer (MD-14).

    Comes off the same scan the graph reads, so the popup is free once the walk
    has happened and can never suggest a note the graph does not know about.
    """
    notes = scan["notes"]
    paths = list(notes)
    index = _candidate_index(paths)
    rows = []
    for rel in sorted(notes):
        row = notes[rel]
        rows.append({
            "rel": rel,
            "path": _client_join(root, rel),
            "title": _display_title(rel),
            "link": _link_form(rel, paths, index),
            "headings": row["headings"],
        })
    return {
        "error": None,
        "root": _client_path(root),
        "notes": rows,
        "assets": scan["assets"],
        "truncated": scan["truncated"],
        "parser_version": PARSER_VERSION,
    }


# ----------------------------------------------------------- graph assembly
#
# Tier 2 (MD-8): nodes and edges are built from the cached rows on every
# request and never stored. Cheap — milliseconds for thousands of notes — and
# it is what keeps a rename honest, because the resolution it depends on
# happens here.


def _graph_nodes_and_edges(root: str, scan: dict):
    notes = scan["notes"]
    note_index = _candidate_index(notes)
    asset_index = _candidate_index(list(notes) + scan["assets"])

    nodes = {}
    for rel in sorted(notes):
        nodes[rel] = {
            "id": rel,
            "kind": "note",
            "label": _display_title(rel),
            # The note's folder relative to the scan root, "" at the top. The
            # canvas lays notes out in one horizontal band per folder, and this
            # is what it bands on — stated outright rather than left for the
            # client to recover by splitting an id, so "a node's id happens to
            # be its relative path" stays an implementation detail of this
            # module instead of becoming a wire contract.
            "dir": rel.rsplit("/", 1)[0] if "/" in rel else "",
            "path": _client_join(root, rel),
            "degree": 0,
        }

    seen = set()
    edges = []

    def add(source, target, kind):
        key = (source, target, kind)
        if key in seen:
            return
        seen.add(key)
        edges.append({"source": source, "target": target, "kind": kind})

    for rel in sorted(notes):
        row = notes[rel]
        for link in _resolved_links(rel, row, note_index, asset_index):
            target = link["rel"]
            if target is None:
                # An unresolved target is one ghost per NAME, so five notes
                # linking `[[Roadmap]]` share the node they are all asking for.
                # Nothing that could never be a note gets one, and with no node
                # there is no edge either.
                if not _ghostable(link["target"]):
                    continue
                ghost = "ghost:" + _normalize_target(link["target"]).lower()
                nodes.setdefault(ghost, {
                    "id": ghost, "kind": "ghost", "label": link["target"],
                    # The authored target, alongside the DISPLAY label: creating
                    # the note is a path operation and must not be driven by
                    # whatever happens to be drawn on the canvas.
                    "target": _normalize_target(link["target"]),
                    # No folder: it does not exist yet, so it is in none. `None`
                    # rather than `""` — the top folder is a real place and a
                    # ghost is not in it.
                    "dir": None,
                    "path": None, "degree": 0})
                add(rel, ghost, "embed" if link["embed"] else "link")
            elif target in nodes:
                add(rel, target, "embed" if link["embed"] else "link")
            # else: the target is an asset (a picture), which is not a note and
            # would otherwise dominate a vault full of screenshots.

    return nodes, edges


def _neighbourhood(edges, focus: str, depth: int):
    """BFS `depth` hops out from `focus`, following edges in both directions —
    what a local graph means (an inbound link is as much a neighbour as an
    outbound one). Adjacency comes from the edges alone; the node table adds
    nothing a BFS can use."""
    adjacent = {}
    for edge in edges:
        adjacent.setdefault(edge["source"], set()).add(edge["target"])
        adjacent.setdefault(edge["target"], set()).add(edge["source"])
    kept = {focus}
    frontier = {focus}
    for _ in range(max(0, depth)):
        nxt = set()
        for node in frontier:
            nxt |= adjacent.get(node, set()) - kept
        if not nxt:
            break
        kept |= nxt
        frontier = nxt
    return kept


def _graph_payload(root: str, scan: dict, focus, depth: int) -> dict:
    """`depth` has three regions, and all three are reachable from a URL:

    * `depth >= 1` with a focus — the local panel: a BFS neighbourhood.
    * `depth == 0` — with a focus, just the focus node (`_neighbourhood` keeps
      only `{focus}`); with no focus, the whole vault. The folder-level `graph`
      mode sends exactly this: `depth: "0"` and no `file`.
    * `depth < 0` — **the whole vault even when a focus is set**, which is the
      note panel's `all` option. A negative sentinel rather than reusing `0`
      because `0`-with-a-focus already means something (one node), and the
      focus is still reported in the payload so the canvas keeps drawing it
      apart from its neighbours.
    """
    nodes, edges = _graph_nodes_and_edges(root, scan)
    if depth >= 0 and focus is not None and focus in nodes:
        kept = _neighbourhood(edges, focus, depth)
        nodes = {node: row for node, row in nodes.items() if node in kept}
        edges = [e for e in edges if e["source"] in nodes and e["target"] in nodes]
    for edge in edges:
        nodes[edge["source"]]["degree"] += 1
        nodes[edge["target"]]["degree"] += 1
    return {
        "error": None,
        "root": _client_path(root),
        "focus": focus,
        "depth": depth,
        "nodes": [nodes[node] for node in sorted(nodes)],
        "edges": edges,
        "total_notes": len(scan["notes"]),
        "truncated": scan["truncated"],
        "parser_version": PARSER_VERSION,
    }


def _error(kind: str, message: str) -> dict:
    return {"error": kind, "message": message}


ACTIONS = ("note", "candidates", "graph")


def main(action: str = "note", file: str = "", root: str = "", depth=1):
    """The template's one entry point.

    `note` answers the note view, `candidates` the `[[` autocomplete, and
    `graph` both graph surfaces — the local panel (with `file` + `depth`) and
    the folder-level mode (root only). Every one of them refuses a mount-backed
    root (MD-11); reading and writing a single file is not affected, because
    that is one bounded read and one bounded write.

    Without an explicit `root`, the scan root is the nearest ancestor of the
    note carrying a vault marker (`vault_root`), falling back to the note's own
    folder. `depth` is untyped on purpose: it arrives as a string from a URL
    param and is coerced here (`_coerce_depth`); a negative value means "the
    whole vault, focus and all" (`_graph_payload`).
    """
    if action not in ACTIONS:
        return _error("bad_action", f"unknown action {action!r}")
    if action == "note" and (not file or not os.path.isabs(file)):
        return _error("bad_request", "'file' must be an absolute path")
    if action in ("candidates", "graph") and not root and not file:
        return _error("bad_request", f"'root' is required for {action}")

    # An explicit `root` always wins; the ascent only supplies the DEFAULT.
    root = os.path.abspath(root) if root else _default_root(file)
    depth = _coerce_depth(depth)
    try:
        _refuse_mounts(root)
    except MountUnsupported as exc:
        return _error("mount_unsupported", str(exc))

    rel = None
    if file:
        file = os.path.abspath(file)
        if not os.path.isfile(file):
            if action == "note":
                return _error("not_found", f"no such file: {file}")
        else:
            rel = os.path.relpath(file, root).replace(os.sep, "/")
            if rel == ".." or rel.startswith("../"):
                if action == "note":
                    return _error("outside_root", f"{file} is not under {root}")
                rel = None

    try:
        scan = scan_indexed(root)
    except MountUnsupported as exc:
        return _error("mount_unsupported", str(exc))
    if action == "candidates":
        return _candidates_payload(root, scan)
    if action == "graph":
        return _graph_payload(root, scan, rel, depth)
    # `note` is the only action that can reach here, and its two early returns
    # above mean `rel` was assigned — but say it rather than assume it, so the
    # invariant survives a fourth action being added.
    if rel is None:
        return _error("not_found", f"no such note: {file}")
    return _note_payload(root, rel, scan)
