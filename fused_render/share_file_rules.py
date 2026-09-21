"""Resolve a file's viewer UDF from the Fused catalog (SPEC `artifact-fileudf.md §2-3`).

`share_app.py` shares apps by delegating to one fixed viewer,
`Fused_App_File`. Sharing a plain file needs to pick the RIGHT viewer for
its extension first — the same one the hosted workbench would open it
with — and that mapping lives nowhere but the catalog: each file-preview UDF
declares what it opens in its own README (a `<!--fused:filePreview-->`
block), and Fused exposes that as metadata on the catalog record
(`fused:filePreview`, `fused:filePreviewExtensions`, `fused:filePreviewFileName`,
`fused:filePreviewRegex`, `fused:filePreviewMenuOrder`, `fused:sharedToken`).

`build_rules()` calls the SDK (`fused.api.get_udfs(whose=…)` for `community`
then `team`) and reduces the catalog to `{name, token, extensions, file_name,
regex, order}` — hence the lazy `import fused` inside it, so this module
stays importable (and testable) with no SDK installed. It runs in the shim
subprocess only, via `_fused_share_app.py`'s `{"action": "rules"}` branch.

`resolve()` is pure and network-free: it mirrors the workbench's own
ordering (`compareUdfRulesByMatchSpecificity`) exactly, so the page a reader
shares is the file the workbench would already show them.
"""
from __future__ import annotations

import json
import os
import re
import time

STATE_DIR = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
CACHE_FILE = "share_file_rules.v1.json"

# A cache the interactive caller (status/lookup while a dialog is open) will
# happily serve stale for a day; publish needs a table that will not go stale
# mid-request, so it asks for one effectively unbounded — see
# artifact-fileudf.md §2.1's table of callers.
RULES_TTL_INTERACTIVE = 24 * 60 * 60.0
RULES_TTL_PUBLISH = 365 * 24 * 60 * 60.0

# Kept whether or not the catalog answered: a `.fused` file is a zip holding
# a fused-render app, and Fused_App_File unpacks and shows it. Appended only
# when no catalog rule already claims the extension (§2.1).
BUILTIN_RULES: list[dict] = [
    {
        "name": "Fused_App_File",
        "token": "UDF_Fused_App_File",
        "extensions": ["fused"],
        "file_name": None,
        "regex": None,
        "order": 1,
    },
]


def _cache_path() -> str:
    return os.path.join(STATE_DIR, CACHE_FILE)


def _extract_rule(record: dict) -> dict | None:
    meta = record.get("metadata") if isinstance(record, dict) else None
    if not isinstance(meta, dict) or not meta.get("fused:filePreview"):
        return None
    name = record.get("name")
    if not name:
        return None
    token = meta.get("fused:sharedToken") or f"UDF_{name}"
    extensions = meta.get("fused:filePreviewExtensions")
    if not isinstance(extensions, list):
        extensions = []
    order_raw = meta.get("fused:filePreviewMenuOrder")
    order = None
    if order_raw is not None and str(order_raw).strip() != "":
        try:
            order = int(order_raw)
        except (TypeError, ValueError):
            order = None
    return {
        "name": name,
        "token": token,
        "extensions": [str(e).lower() for e in extensions],
        "file_name": meta.get("fused:filePreviewFileName") or None,
        "regex": meta.get("fused:filePreviewRegex") or None,
        "order": order,
    }


def _iter_udf_records(udfs):
    """`fused.api.get_udfs(whose=...)` returns a `UdfRegistry` — a dict-like
    mapping `str -> Udf` (NOT a list of dicts: iterating it directly yields
    just the name strings, and `dict(name_string)` is the
    `dictionary update sequence element ... length 1; 2 is required` crash
    this traversal exists to avoid). Prefer `.items()`; fall back to treating
    `udfs` as a plain iterable of objects/dicts each carrying their own
    `name`, for the fixture shape older tests used and any other iterable
    the SDK might someday hand back."""
    if hasattr(udfs, "items"):
        return udfs.items()
    out = []
    for record in udfs:
        name = record.get("name") if isinstance(record, dict) else getattr(record, "name", None)
        out.append((name, record))
    return out


def _record_to_dict(name, record) -> dict:
    """Normalize one catalog entry to the plain `{name, metadata}` shape
    `_extract_rule` expects. A real `Udf` exposes `metadata` as an
    ATTRIBUTE (a dict) — `record.get("metadata")` silently returns nothing
    for it, so a plain-dict `.get` is only correct for the plain-dict
    fixture shape; a real record needs `getattr`."""
    if isinstance(record, dict):
        return {"name": record.get("name", name), "metadata": record.get("metadata")}
    return {"name": getattr(record, "name", name), "metadata": getattr(record, "metadata", None)}


def build_rules() -> list[dict]:
    """Ask Fused for the community then team catalogs, keep file-preview
    records, team rules ahead of community ones at equal specificity (a team
    override wins). Network + SDK; only ever called from the shim subprocess."""
    import fused  # local: the SDK is not importable in the server process

    rules: list[dict] = []
    seen: set[tuple] = set()
    for whose in ("team", "community"):
        try:
            udfs = fused.api.get_udfs(whose=whose)
        except Exception:
            udfs = None
        if not udfs:
            continue
        for name, record in _iter_udf_records(udfs):
            rule = _extract_rule(_record_to_dict(name, record))
            if rule is None:
                continue
            key = (rule["name"], whose)
            if key in seen:
                continue
            seen.add(key)
            rules.append(rule)
    return rules


def _with_builtin(rules: list[dict]) -> list[dict]:
    covered = {ext for rule in rules for ext in rule.get("extensions") or []}
    out = list(rules)
    for builtin in BUILTIN_RULES:
        if not any(ext in covered for ext in builtin["extensions"]):
            out.append(dict(builtin))
    return out


def _read_cache() -> tuple[list[dict] | None, float | None]:
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    rules = data.get("rules")
    built_at = data.get("built_at")
    if not isinstance(rules, list) or not isinstance(built_at, (int, float)):
        return None, None
    return rules, float(built_at)


def _write_cache(rules: list[dict]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = _cache_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"built_at": time.time(), "rules": rules}, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_rules(ttl: float = RULES_TTL_INTERACTIVE) -> list[dict]:
    """The rule table, from a fresh-enough cache or freshly built. A missing
    cache is always built regardless of `ttl` (artifact-fileudf.md §2.1)."""
    rules, built_at = _read_cache()
    if rules is None or built_at is None or (time.time() - built_at) > ttl:
        rules = build_rules()
        _write_cache(rules)
    return _with_builtin(rules)


def _matches(rule: dict, path: str) -> bool:
    """True if `rule` claims this path by ANY predicate (filename, regex, or
    extension) — every predicate is basename-scoped (finding 5: a regex was
    matched against the whole absolute path, so a rule with `regex: "census"`
    fired for `/Users/me/census_data/notes.txt`)."""
    return _specific_match(rule, path) or _extension_match(rule, path)


def _specific_match(rule: dict, path: str) -> bool:
    """True if the filename or regex predicate itself matched THIS path —
    not merely whether the rule declares one (finding 6: a rule with
    `regex` plus `extensions` outranked a purpose-built, lower-`order` rule
    even when its regex never matched and both only matched by extension).
    Basename-scoped, matching every other predicate here and the workbench's
    own `compareUdfRulesByMatchSpecificity`."""
    basename = os.path.basename(path)
    file_name = rule.get("file_name")
    if file_name:
        # `fused:filePreviewFileName` is a list in the real catalog (e.g.
        # `["_sample"]`), not the bare string this used to assume; a str
        # fixture is still accepted. Any element matching is enough.
        names = file_name if isinstance(file_name, list) else [file_name]
        if any(isinstance(n, str) and n.lower() == basename.lower() for n in names):
            return True
    regex = rule.get("regex")
    if regex:
        try:
            if re.search(regex, basename):
                return True
        except re.error:
            pass
    return False


def _extension_match(rule: dict, path: str) -> bool:
    # A rule CAN declare the empty string as an extension (the real catalog
    # has one: `Empty_Extension_File`). This is deliberately left able to
    # match — `os.path.splitext` already scopes it to paths that genuinely
    # have no extension (`Makefile`, `.bashrc`) rather than to every path or
    # every dotfile-with-a-suffix (`.env.local` still gets `.local`), so an
    # empty-extension rule cannot accidentally swallow ordinary files.
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    extensions = rule.get("extensions") or []
    return ext in extensions


def _sort_key(rule: dict, path: str):
    order = rule.get("order")
    # A rule without `Menu order` sorts after every rule that has one.
    order_key = (1, 0) if order is None else (0, order)
    # Specificity is computed against THIS path, not the rule in the
    # abstract — a rule only outranks by filename/regex when that predicate
    # is what actually matched here (finding 6).
    return (0 if _specific_match(rule, path) else 1, order_key, rule.get("name") or "")


def resolve(path: str, rules: list[dict]) -> dict | None:
    """`compareUdfRulesByMatchSpecificity`, mirrored exactly (artifact-fileudf.md §3):
    filter to matching rules, sort by (filename/regex beats extension, then
    Menu order ascending with missing last, then name), take the first."""
    matching = [r for r in rules if _matches(r, path)]
    if not matching:
        return None
    matching.sort(key=lambda r: _sort_key(r, path))
    return matching[0]
