"""The fused page API's version, and the task that migrates an app to it.

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
offered exactly where it might be needed.

THE CHANGELOG IS A SKILL. `skills/fused-render-api-migration/` holds one
`docs/v{N}.md` per version — what changed in N over N-1, in text a Claude
session can act on — and a SKILL.md that knows which notes to read for which
jump and how to do the whole migration (sweep the folder, rewrite, bump the
tag). The server does not carry a second copy of that knowledge: the current
version here is read off that same `docs/` folder (through `skill_sources`,
so a dev checkout and a wheel install agree), and the migration task's prompt
is one line that invokes the skill. Shipping a new API version is therefore
a new `docs/vN.md` plus the starter's tag (`app_starter/index.html`) bumped to
match — the two must agree, or the starter reads as "ahead of current".

Nothing here reads a whole page — the tag is matched from the same 4 KiB head
budget as `app_listing.has_fused_meta`.
"""
import os
import re

from fused_render.skill_sources import skill_sources

META_NAME = "fused-api-version"

# The skill that owns the changelog and the migration procedure; the plugin
# root (`skill_plugin`, D216) hands it to every session we spawn under the
# `fused-render:` prefix.
MIGRATION_SKILL = "fused-render-api-migration"
MIGRATION_SKILL_QUALIFIED = f"fused-render:{MIGRATION_SKILL}"
_DOCS_SUBDIR = "docs"
_DOC_RE = re.compile(r"^v(\d+)\.md$")

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


def docs_dir() -> str | None:
    """The migration skill's `docs/` folder, or None when the skill is not
    resolvable (a checkout with no skills at all)."""
    src = skill_sources().get(MIGRATION_SKILL)
    if not src:
        return None
    d = os.path.join(src, _DOCS_SUBDIR)
    return d if os.path.isdir(d) else None


def versions() -> list[int]:
    """Every documented API version — one `docs/v{N}.md` each — ascending.
    Empty when the skill or its docs cannot be found."""
    d = docs_dir()
    if d is None:
        return []
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        m = _DOC_RE.match(n)
        if m and os.path.isfile(os.path.join(d, n)):
            v = int(m.group(1))
            if v > 0:
                out.append(v)
    return sorted(out)


def current_version() -> int:
    """The API version the runtime speaks now — the highest documented one.
    0 only when no docs resolve, which also switches the Migrate button off
    (nothing is "behind" version 0)."""
    vs = versions()
    return vs[-1] if vs else 0


def migration_prompt(entry_html: str, from_version: int, to_version: int) -> str:
    """The migration task's text: invoke the skill, name the jump. The skill
    carries the procedure and the per-version notes; repeating them here
    would be the second copy the skill exists to prevent."""
    entry_name = os.path.basename(entry_html)
    return (
        f"Migrate this fused-render app from fused API version {from_version} "
        f"to version {to_version}. Invoke the `{MIGRATION_SKILL_QUALIFIED}` "
        f"skill and follow it end to end — it reads the per-version notes, "
        f"updates every file in this folder that uses the `fused` runtime, and "
        f"bumps the `<meta name=\"{META_NAME}\">` tag in `{entry_name}` (this "
        f"file, the app's entry page). Do not change anything the skill's notes "
        f"do not ask for."
    )
