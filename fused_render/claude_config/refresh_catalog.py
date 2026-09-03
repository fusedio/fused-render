"""Catalog refresh (preferences.md §5).

Fetches Anthropic's settings reference markdown, parses each settings-key
section, and rewrites the doc/default/minVersion half of settings_catalog.json
— keyed by each entry's `docKey` (falling back to `key`). The curated overlay
fields (label/group/control/options/docKey/unsetLabel) are preserved untouched.

READ from lib.load_catalog() — the merge, not a single file: PACKAGED curated
fields (current as of this install) with whatever doc/default/minVersion a
PRIOR refresh already wrote to the override, per key. Starting from the merge
rather than from whichever raw file catalog_read_path() names matters here
specifically: an entry the packaged copy added or changed since the last
refresh (a new `select` option, say) would otherwise get baked into the new
override snapshot with its STALE curated half, the same staleness this module
exists to avoid on the doc half. WRITE only to lib.catalog_override_path(). It
used to rewrite the file in place; it cannot any more, because the shipped
copy now lives in site-packages / inside a signed .app — see lib.py's catalog
section for why the split exists rather than a single mutable path.

`default` comes ONLY from the `**Default**: X` bullet — never invented, never
read off the worked example (§1 honesty rule). Undocumented surfaced keys keep
their existing doc/default and are reported as warnings.

Two triggers, one code path (preferences.md §5):
  - callable action:  main() -> {ok, updated, total, undocumented[], error?}
    (never sys.exit/print for control flow — the caller is an HTTP handler that
    must render the outcome). Explicit, user-triggered (the Preferences "Refresh
    catalog" button), never on load.
  - CLI:  python3 -m fused_render.claude_config.refresh_catalog
    (the __main__ wrapper prints the same result).
"""
import json
import re
import urllib.request
from typing import Any, Optional

from . import lib

# Anthropic restructured the docs (2026-08): the old SETTINGS_URL page
# (settings.md) is now prose about settings files, precedence and
# troubleshooting — it has no settings table any more, 0 rows. The key
# reference moved here.
SETTINGS_URL = "https://code.claude.com/docs/en/settings-reference.md"
# Bounded so a slow/hung docs host returns {ok:false, error} the button can
# render, instead of pinning a threadpool worker until the client gives up.
FETCH_TIMEOUT = 20

# The new page is not a table — it is one `### \`key\`` heading per setting
# (215 of them as of this rewrite), each followed by a prose paragraph and
# then a bullet list (`* **Scope**: …`, `* **Type**: …`, `* **Default**: …`,
# `* **Per-session overrides**: …`). Dotted sub-keys get their own headings
# (`### \`permissions.allow\``), which existing docKey lookups already expect.
#
# `#### Fields for \`modelPicker\`` (4 hashes) is NOT a key — it documents the
# object shape of one key's value. The heading regex requires exactly three
# `#` then a space then a backtick, which a 4-hash line can never match
# (its 4th character is `#`, not a space), so these are skipped without a
# special case.
_KEY_HEADING = re.compile(r"^### `([A-Za-z][A-Za-z0-9_.]*)`\s*$", re.M)
# Every section's bullet list opens with Scope (verified 1:1 against the
# heading count), so it is the boundary between the prose paragraph above it
# and the bullets below — clean_doc only wants the former.
_SCOPE_BULLET = re.compile(r"^\* \*\*Scope\*\*", re.M)
_DEFAULT_BULLET = re.compile(r"^\* \*\*Default\*\*:\s*(.*)$", re.M)
_BACKTICK = re.compile(r"`([^`]+)`")
# "min-version:" prose is gone from the new page entirely (0 matches) — the
# only surviving min-version signal is "Requires Claude Code vX.Y.Z or later",
# still written inline in a key's own paragraph (74 occurrences), which is why
# this searches the same paragraph clean_doc reads rather than the whole page.
_MINVER = re.compile(r"Requires Claude Code v([\d.]+)")

# Docs document ~215 keys today (was ~180 on the old table); a tiny result
# means the parser missed the page's structure — don't clobber the catalog.
_MIN_KEYS = 100


def coerce_default(token: str) -> Any:
    tok = token.strip().strip("`").strip()
    try:
        return json.loads(tok)
    except ValueError:
        return tok.strip('"')


def extract_default(desc: str) -> Any:
    """The `**Default**` bullet's value, or None when the docs don't hand us
    one to be honest about (§1). Three shapes, in the order they're checked:

      * "unset[, so …]" — Anthropic's own word for "no default"; the honest
        value is None, not the literal string "unset". This has to run BEFORE
        the backtick search below: several "unset" bullets go on to mention
        an unrelated backticked setting or path in their explanation (e.g.
        "unset, so Claude Code finds `bwrap` on `PATH`"), and reading THAT as
        the default would misattribute someone else's value to this key.
      * a backticked token — Anthropic's own literal value spelled as JSON
        (`` `false` ``, `` `true` ``) — taken as-is, coerced the same way the
        table's example column used to be.
      * plain prose with neither ("not locked") — kept verbatim up to the
        first explanatory clause rather than coerced into a value nobody
        wrote down.
    """
    m = _DEFAULT_BULLET.search(desc)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.startswith("unset"):
        return None
    tick = _BACKTICK.search(raw)
    if tick:
        return coerce_default(tick.group(1))
    cut = re.split(r"[.;,]", raw, maxsplit=1)[0].strip()
    return cut or None


def extract_min_version(desc: str) -> Optional[str]:
    m = _MINVER.search(desc)
    return m.group(1) if m else None


def clean_doc(desc: str) -> str:
    """The prose paragraph before the bullet list, reduced to one sentence.
    Unlike the old table-cell version, there is no leading `**Default**:` to
    strip — the new page keeps that in its own bullet, never inline in the
    description — so this is markdown cleanup and a first-sentence cut only."""
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", desc)
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"\s+", " ", s).strip()
    dot = s.find(". ")
    if dot != -1:
        s = s[: dot + 1]
    return s


def parse_settings_reference(md: str) -> dict:
    """One entry per key heading section (### followed by a backticked key)
    — see the module comment for the page's shape. Raises ValueError only
    when the page has changed
    shape so completely that not even one heading matches (the same "docs
    moved under us" signal the old table-marker check gave); main() is what
    turns that into {ok: False, error} rather than a 500."""
    headings = list(_KEY_HEADING.finditer(md))
    if not headings:
        raise ValueError("found no '### `key`' headings")
    out = {}
    for i, m in enumerate(headings):
        key = m.group(1)
        if key in out:  # first occurrence wins
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(md)
        body = md[m.end():end]
        scope = _SCOPE_BULLET.search(body)
        # The prose paragraph is everything before the bullet list; a section
        # missing a Scope bullet (none observed, but third-party docs drift)
        # falls back to reading the whole body rather than dropping the doc.
        prose = body[: scope.start()] if scope else body
        out[key] = {
            "doc": clean_doc(prose),
            "default": extract_default(body),
            "minVersion": extract_min_version(prose),
        }
    return out


def _fetch() -> str:
    """The docs markdown. Its own function so a test can supply a fixed page:
    the parser, the sanity floor and the write path are all worth covering, and
    none of them should need the network to be reachable."""
    # A User-Agent is required — the docs host 403s the default urllib UA.
    req = urllib.request.Request(SETTINGS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        return r.read().decode("utf-8")


def main() -> dict:
    """The dispatch entry point (preferences.md §5); returns a JSON result.
    On any failure the existing catalog is left untouched (§5 no-silent-truncation)
    and {ok:false, error} is returned — never raised — so the button surfaces it.

    Both the fetch AND the parse/read/write are wrapped: a parser that raises
    on a reshaped page (parse_settings_reference's ValueError) or a catalog
    read that hits a corrupt override file used to escape this function
    entirely and reach the HTTP router as a raw 500 — the button rendered
    Python's exception text instead of an in-band error."""
    try:
        md = _fetch()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"fetch failed ({e}); kept existing catalog"}

    try:
        entries = parse_settings_reference(md)
        if len(entries) < _MIN_KEYS:
            return {"ok": False,
                     "error": f"parsed only {len(entries)} keys (expected ~215); "
                              "docs shape changed"}

        catalog = lib.load_catalog()

        updated, undocumented = 0, []
        for d in catalog:
            dk = d.get("docKey") or d["key"]
            e = entries.get(dk)
            if not e:
                undocumented.append(d["key"])
                continue
            d["doc"], d["default"], d["minVersion"] = e["doc"], e["default"], e["minVersion"]
            updated += 1

        # The override, never the packaged copy. Atomic (write-then-replace) via
        # lib.write_json, which also mkdir -p's the claude-config/ dir on first use.
        lib.write_json(lib.catalog_override_path(), catalog)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"parse failed ({e}); kept existing catalog"}

    return {"ok": True, "updated": updated, "total": len(catalog),
            "undocumented": undocumented, "path": lib.catalog_override_path()}


if __name__ == "__main__":
    import sys
    res = main()
    if not res["ok"]:
        sys.exit(f"✗ {res['error']}")
    print(f"✓ refreshed {res['updated']}/{res['total']} catalog entries from docs")
    if res["undocumented"]:
        print(f"⚠ {len(res['undocumented'])} surfaced key(s) undocumented (kept existing): "
              f"{', '.join(res['undocumented'])}")
