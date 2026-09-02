"""The fused page API's version, and how an app on an older one migrates.

The `fused` runtime a page is handed (`fused.ai`, `fused.runPython`,
`fused.params`, ...) changes over time, and a hard break like the `fused.ai`
namespace rebuild (D631-D633) leaves every app authored earlier broken at its
first call, with nothing on disk saying WHICH shape it was written against.
So an app's entry page declares the API version it was authored for, beside
the app marker itself:

    <meta name="fused-app" />
    <meta name="fused-api-version" content="1" />

A page without the tag is version 0 — every app authored before the tag
existed. Absence is a fact about age, not something a migration stamps over:
`meta_migration` and the community install deliberately leave it alone, so
"missing" keeps meaning "predates versioning" and the migrate button is
offered exactly where it might be needed. (A page that already uses the new
shapes but never got the tag also reads 0; the migration prompt tells the
session to verify what the code actually calls and, if it is already current,
to just add the tag.)

`fused_api_migrations.json` beside this module is the changelog: one entry per
version, keyed by the version number as a string, holding plain text
describing what changed in that version over the one before. `CURRENT` is the
largest key, so shipping a new API version is ONE JSON entry — plus the
starter's tag (`app_starter/index.html`) bumped to match (the two must agree:
a starter declaring a version the changelog does not know would read as "ahead
of current" and the button would stay off). Migrating an app from
version A to version B hands the session every entry in (A, B], in order:
v2 → v5 attaches v3 + v4 + v5. Nothing here reads the whole file — the tag is
matched from the same 4 KiB head budget as `app_listing.has_fused_meta`.

BUMPING THE VERSION (the procedure the next breaking fused-API PR follows):
1. add a `"N"` entry to `fused_api_migrations.json` describing the break in
   text a migrating session can act on — old spelling → new spelling;
2. set `content="N"` on the starter's `<meta name="fused-api-version">`;
3. nothing else — `CURRENT`, the app page's button, and the prompt all read
   the JSON.
"""
import json
import os
import re

META_NAME = "fused-api-version"

_MIGRATIONS_JSON = os.path.join(os.path.dirname(__file__), "fused_api_migrations.json")

# Same head budget as `app_listing.has_fused_meta`: the tag sits beside the app
# marker at the top of <head>, and an unbounded read per app is a full-file
# scan of every page on every listing.
_META_SCAN_BYTES = 4096

# Two steps, deliberately: find the whole <meta ...> tag that names us, then
# read `content` out of THAT tag — `content="1" name="fused-api-version"` is
# legal HTML and a single left-to-right regex would miss it.
_TAG_RE = re.compile(
    rb"<meta\s[^>]*name\s*=\s*[\"']?fused-api-version[\"']?[^>]*>", re.IGNORECASE)
_CONTENT_RE = re.compile(
    rb"content\s*=\s*[\"']?\s*(\d+)\s*[\"']?", re.IGNORECASE)


def version_from_text(head: bytes | str) -> int:
    """The declared API version in a page's head bytes; 0 when the tag is
    absent or its content is not a whole number."""
    if isinstance(head, str):
        head = head[:_META_SCAN_BYTES].encode("utf-8", "ignore")
    else:
        head = head[:_META_SCAN_BYTES]
    tag = _TAG_RE.search(head)
    if not tag:
        return 0
    m = _CONTENT_RE.search(tag.group(0))
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def api_version(html_path: str) -> int:
    """The API version an entry page declares, 0 when undeclared. Never
    raises — an unreadable page is an undeclared one."""
    try:
        with open(html_path, "rb") as fh:
            head = fh.read(_META_SCAN_BYTES)
    except OSError:
        return 0
    return version_from_text(head)


def migrations() -> dict[int, dict]:
    """The changelog, keyed by integer version, ascending. Read on each call
    (it is one small file) so a dev checkout editing the JSON sees it live."""
    with open(_MIGRATIONS_JSON, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    out: dict[int, dict] = {}
    for k, v in raw.items():
        try:
            n = int(k)
        except (TypeError, ValueError):
            continue
        if n <= 0 or not isinstance(v, dict):
            continue
        out[n] = v
    return dict(sorted(out.items()))


def current_version() -> int:
    """The API version the runtime speaks now — the largest changelog entry.
    0 only if the changelog is empty."""
    m = migrations()
    return max(m) if m else 0


def migration_context(from_version: int, to_version: int) -> str:
    """Every changelog entry in (from_version, to_version], oldest first, as
    one text block headed per version. Empty when nothing lies between."""
    parts = []
    for n, entry in migrations().items():
        if from_version < n <= to_version:
            summary = str(entry.get("summary") or "").strip()
            changes = str(entry.get("changes") or "").strip()
            head = f"## fused API version {n}"
            if summary:
                head += f" — {summary}"
            parts.append(head + "\n\n" + changes)
    return "\n\n".join(parts)


def migration_prompt(entry_html: str, from_version: int, to_version: int) -> str:
    """The task text a migrating Claude session is handed: what to move, the
    tag to end on, and the changelog for every version being crossed."""
    entry_name = os.path.basename(entry_html)
    tag = f'<meta name="{META_NAME}" content="{to_version}" />'
    lines = [
        f"Migrate this fused-render app from fused API version {from_version} "
        f"to version {to_version}.",
        "",
        f"The app's entry page is `{entry_name}` (this file). The fused page API "
        "it was written against has changed; the notes below describe every "
        "change between the version it declares and the current one, oldest "
        "first. Apply them all.",
        "",
        "Steps:",
        f"1. Read `{entry_name}` and every other `.html` and `.py` file in this "
        "folder that uses the `fused` runtime (`fused.*` in pages, `import "
        "fused_ai` / `fused_ai.*` in Python) and update each call and each "
        "reader of a result to the new shapes described below. Verify what the "
        "code ACTUALLY calls before changing it: a page may already be on the "
        "new shapes without declaring so, in which case only step 2 applies.",
        f"2. In `{entry_name}`, declare the new version by placing "
        f"`{tag}` directly after the `<meta name=\"fused-app\" />` tag "
        "(replace any existing `fused-api-version` meta; keep both tags near "
        "the top of <head>, inside the first 4 KiB of the file).",
        "3. Do not change behaviour beyond what the notes require, and do not "
        "restyle or restructure the app.",
        "",
        "Changes to apply:",
        "",
        migration_context(from_version, to_version) or "(no recorded changes)",
    ]
    return "\n".join(lines)
