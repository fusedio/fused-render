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

# Bumped whenever parse_note's output shape or semantics change, so a stored
# index row from an older parser is invalidated wholesale (MD-8).
PARSER_VERSION = 1

# What counts as a note. Both are bound to this template in registry.json.
NOTE_SUFFIXES = (".md", ".markdown")

# A file bigger than this is not a note (a generated changelog, a dumped
# dataset with an .md name); reading it would dominate the walk. Reported, not
# silently dropped.
MAX_NOTE_BYTES = 256 * 1024

# Hard caps on a single walk. Exceeding either is reported so the UI can say
# the graph is partial rather than quietly showing a subset (MD-10).
MAX_FILES = 5000
MAX_ASSETS = 5000

# Directories a note vault never means to include. Dotdirs cover .git/.obsidian;
# the rest are the usual vendored trees whose bundled markdown is noise.
SKIP_DIRS = frozenset(("node_modules", "__pycache__", "site-packages", "venv"))


class MountUnsupported(Exception):
    """Raised instead of walking a mount-backed root (MD-11)."""


# --------------------------------------------------------------- mount refusal


def _refuse_mounts(root: str) -> None:
    """Refuse a mount-backed root outright.

    The detector is the app's own `shell.mounts.is_mount_backed` — the same one
    `server._run_condition` and every fs route use — rather than a second copy
    of the rule. An ImportError means we cannot tell, and "cannot tell" must
    read as "refuse": a walk we were not allowed to do is the failure this
    exists to prevent.
    """
    try:
        from fused_render.shell.mounts import is_mount_backed
    except Exception as exc:  # noqa: BLE001 — cannot tell -> refuse
        raise MountUnsupported(f"mount detection unavailable: {exc}") from exc
    if is_mount_backed(root):
        raise MountUnsupported(
            "The link graph is not supported on remote mounts. "
            "Opening and editing a single .md file still works.")


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
# A hashtag: not preceded by a word char, `/` or another `#` (so `a#b` and the
# second `#` of `## Heading` are out), and starting with a letter/underscore (so
# `#1234` and a `# ` heading are out).
_TAG = re.compile(r"(?<![\w/#])#(?P<tag>[A-Za-z_][\w/-]*)")
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


def _parse_frontmatter(block: str) -> dict:
    """The handful of frontmatter keys this template reads: `title` and `tags`.

    A deliberately tiny YAML subset (scalar, `[a, b]` flow list, `- a` block
    list) rather than a parser: the two keys are all we act on, and PyYAML is
    not in the bundled set.
    """
    out = {}
    key = None
    for raw in block.split("\n"):
        line = raw.rstrip()
        if not line or line.strip() in ("---", "..."):
            continue
        item = re.match(r"^[ \t]*-[ \t]+(?P<value>.+)$", line)
        if item is not None and key is not None:
            out.setdefault(key, []).append(item.group("value").strip().strip("'\""))
            continue
        field = re.match(r"^(?P<key>[A-Za-z_][\w-]*)[ \t]*:[ \t]*(?P<value>.*)$", line)
        if field is None:
            continue
        key = field.group("key").lower()
        value = field.group("value").strip()
        if not value:
            out.setdefault(key, [])
            continue
        if value.startswith("[") and value.endswith("]"):
            out[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        else:
            out[key] = value.strip("'\"")
        key = key if isinstance(out.get(key), list) else None
    return out


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

    Returns `{"title", "headings", "tags", "links"}`. `links` carries the RAW
    authored target (MD-6) in document order; `title` is None when the note
    states none (the caller falls back to the filename, which parse_note does
    not know).
    """
    frontmatter = _frontmatter_span(text)
    body = _mask_code(text, frontmatter)
    meta = _parse_frontmatter(text[frontmatter[0]:frontmatter[1]]) if frontmatter else {}

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

    tags = []
    for match in _TAG.finditer(body):
        tag = match.group("tag").rstrip("/")
        if tag and tag not in tags:
            tags.append(tag)
    front_tags = meta.get("tags") or meta.get("tag") or []
    if isinstance(front_tags, str):
        front_tags = [t.strip() for t in front_tags.split(",")]
    for tag in front_tags:
        tag = str(tag).lstrip("#").rstrip("/").strip()
        if tag and tag not in tags:
            tags.append(tag)

    headings = []
    for line in body.split("\n"):
        match = _HEADING.match(line)
        if match is not None:
            headings.append({"level": len(match.group("hashes")),
                             "text": match.group("text").strip()})

    title = meta.get("title")
    if isinstance(title, list):
        title = title[0] if title else None
    if not title:
        title = next((h["text"] for h in headings if h["level"] == 1), None)
    return {
        "title": title or None,
        "headings": headings,
        "tags": tags,
        "links": links,
    }


# ---------------------------------------------------------------- resolution


def _stem(rel: str) -> str:
    lower = rel.lower()
    for suffix in NOTE_SUFFIXES:
        if lower.endswith(suffix):
            return rel[: -len(suffix)]
    return rel


def _normalize_target(target: str) -> str:
    target = (target or "").strip().replace("\\", "/")
    while target.startswith("./"):
        target = target[2:]
    return target.strip("/")


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
    suffix, then as a bare basename. Case-insensitive, as Obsidian is. `paths`
    is the candidate set that exists *now*; `index` is the memoised
    `_candidate_index` of it when a caller resolves many links at once.
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
    hit = _only([rel for key, rels in index.items() if key.endswith(suffix)
                 for rel in rels])
    if hit is not None:
        return hit
    if "/" in lowered:
        return None
    return _only(index.get(lowered.rsplit("/", 1)[-1]))


# --------------------------------------------------------------------- walking


def _is_note(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in NOTE_SUFFIXES)


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIRS


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def scan_root(root: str, max_bytes: int = MAX_NOTE_BYTES, max_files: int = MAX_FILES,
              max_assets: int = MAX_ASSETS) -> dict:
    """Walk `root` and parse every note under it.

    Returns `{"root", "notes": {relpath: row}, "assets": [relpath], "truncated",
    "skipped_large"}`. Refuses a mount-backed root before walking (MD-11).
    Deterministic: directories and files are visited in sorted order, so a cap
    that fires drops the same tail every time rather than an arbitrary subset.
    """
    root = os.path.abspath(root)
    _refuse_mounts(root)

    notes = {}
    assets = []
    skipped_large = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not _is_note(name):
                if len(assets) < max_assets:
                    assets.append(rel)
                continue
            if len(notes) >= max_files:
                truncated = True
                break
            try:
                stat = os.stat(full)
            except OSError:
                continue
            if stat.st_size > max_bytes:
                skipped_large.append(rel)
                continue
            try:
                text = _read_text(full)
            except OSError:
                continue
            row = parse_note(text)
            row["mtime_ns"] = stat.st_mtime_ns
            row["size"] = stat.st_size
            notes[rel] = row
        if truncated:
            break

    return {
        "root": root,
        "notes": notes,
        "assets": assets,
        "truncated": truncated,
        "skipped_large": skipped_large,
    }


# --------------------------------------------------------------------- the API


def _display_title(rel: str, row) -> str:
    return (row or {}).get("title") or os.path.basename(_stem(rel))


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
        # Present on disk but not in the scan (over the size cap, or excluded);
        # parse it directly so the note view still works.
        row = parse_note(_read_text(os.path.join(root, rel)))

    def absolute(target_rel):
        return os.path.join(root, target_rel.replace("/", os.sep)) if target_rel else None

    links = []
    for link in _resolved_links(rel, row, note_index, asset_index):
        links.append({
            "target": link["target"],
            "heading": link["heading"],
            "label": link["label"],
            "embed": link["embed"],
            "wiki": link["wiki"],
            "path": absolute(link["rel"]),
            "title": _display_title(link["rel"], notes.get(link["rel"])) if link["rel"] else None,
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
                "title": _display_title(other_rel, notes[other_rel]),
                "label": link["label"],
                "heading": link["heading"],
                "embed": link["embed"],
            })
    return {
        "error": None,
        "root": root,
        "rel": rel,
        "title": _display_title(rel, row),
        "headings": row["headings"],
        "tags": row["tags"],
        "links": links,
        "backlinks": backlinks,
        "notes": len(notes),
        "truncated": scan["truncated"],
        "skipped_large": scan["skipped_large"],
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
    """Everything the `[[`, `[[note#` and `#tag` popups offer (MD-14).

    Comes off the same scan the graph reads, so the popup is free once the walk
    has happened and can never suggest a note the graph does not know about.
    """
    notes = scan["notes"]
    paths = list(notes)
    index = _candidate_index(paths)
    tags = set()
    rows = []
    for rel in sorted(notes):
        row = notes[rel]
        tags.update(row["tags"])
        rows.append({
            "rel": rel,
            "path": os.path.join(root, rel.replace("/", os.sep)),
            "title": _display_title(rel, row),
            "link": _link_form(rel, paths, index),
            "headings": row["headings"],
        })
    return {
        "error": None,
        "root": root,
        "notes": rows,
        "tags": sorted(tags),
        "assets": scan["assets"],
        "truncated": scan["truncated"],
        "parser_version": PARSER_VERSION,
    }


def _error(kind: str, message: str) -> dict:
    return {"error": kind, "message": message}


def main(action: str = "note", file: str = "", root: str = ""):
    """The template's one entry point.

    `action="note"` answers the note view; `action="candidates"` answers the
    `[[` autocomplete. Every walk-backed action refuses a mount-backed root
    (MD-11); a single-file read is not affected, because that is what the
    template does through `fused.readFile` anyway.
    """
    if action not in ("note", "candidates"):
        return _error("bad_action", f"unknown action {action!r}")
    if action == "note" and (not file or not os.path.isabs(file)):
        return _error("bad_request", "'file' must be an absolute path")
    if action == "candidates" and not root:
        return _error("bad_request", "'root' is required for candidates")

    root = os.path.abspath(root) if root else os.path.dirname(os.path.abspath(file))
    try:
        _refuse_mounts(root)
    except MountUnsupported as exc:
        return _error("mount_unsupported", str(exc))

    rel = None
    if action == "note":
        file = os.path.abspath(file)
        if not os.path.isfile(file):
            return _error("not_found", f"no such file: {file}")
        rel = os.path.relpath(file, root).replace(os.sep, "/")
        if rel == ".." or rel.startswith("../"):
            return _error("outside_root", f"{file} is not under {root}")

    try:
        scan = scan_root(root)
    except MountUnsupported as exc:
        return _error("mount_unsupported", str(exc))
    if action == "candidates":
        return _candidates_payload(root, scan)
    return _note_payload(root, rel, scan)
