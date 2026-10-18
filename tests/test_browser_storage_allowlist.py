"""D1386 / SPEC §52: browser storage holds signals, caches and per-tab hand-offs,
never a fact the user would miss across a relaunch.

Structural, like `test_activity_bar_structure.py`: it reads source text rather
than rendering anything. Every `.ts` / `.tsx` under `frontend/src` (tests
excluded) that names `localStorage` or `sessionStorage` IN CODE — comments are
stripped first, because several files legitimately explain in prose why they
no longer use it — must be listed below with the category that permits it.
A file that stops touching storage must leave the list, so the list cannot
rot into a grandfather clause.

Categories (SPEC §52 BS-1):
  signal    a cross-window stamp heard through the `storage` event; the value
            is never read as state
  cache     a copy of server-owned state, re-checked against the server
  tab       sessionStorage hand-off or per-tab memory
  display   pre-paint per-browser display choice (theme, D134)
  import    reads an OLD key once to push it to the server, then removes it
  dev       a developer flag set by hand in devtools
"""
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "frontend", "src")

# frontend/src-relative path -> category. Keep alphabetical within a layer.
ALLOWED = {
    # ---- platform ---------------------------------------------------------
    "platform/lib/agent-backends.ts": "signal",
    "platform/lib/bookmarks.ts": "tab",            # armed bookmark (sessionStorage)
    "platform/lib/claude-defaults.ts": "signal",
    "platform/lib/pending-claude-ask.ts": "tab",
    "platform/lib/presence.ts": "signal",
    "platform/lib/restart-store.ts": "signal",
    "platform/lib/theme.ts": "display",
    "platform/lib/tours/index.ts": "display",       # one-shot tour seen (per viewer)
    "platform/lib/ui-layout.ts": "import",
    "platform/ui/IconPicker.tsx": "display",        # picker recents / colour (per viewer)
    "platform/ui/ServerStatusBanner.tsx": "dev",
    "platform/ui/UpdateNotifier.tsx": "tab",
    # ---- shell ------------------------------------------------------------
    "shell/CurrentAppsSection.tsx": "signal",       # desk-changed nudge (+ order import)
    "shell/ScheduleTaskViews.tsx": "tab",           # list memory (+ lane/group import)
    "shell/Scheduled.tsx": "import",
    "shell/TaskCards.tsx": "tab",
    "shell/TerminalDrawer.tsx": "cache",            # pty ids re-checked against GET /api/terminal
    "shell/UpdateCard.tsx": "tab",
    "shell/notifications-seen-store.ts": "import",
    "shell/settings/claude.tsx": "display",         # accordion open/order habits (Akshil 2026-10-20)
    "shell/task-peek-store.ts": "display",          # peek width + per-tab autocollapse marker
    "shell/tasksPulse.ts": "signal",                # chat-activity listener (+ seen import)
    # ---- apps -------------------------------------------------------------
    "apps/ai_models/local/HubSearchScreen.tsx": "cache",
    "apps/bots/lib/layout.ts": "display",           # bots column layout (per viewer)
    "apps/bots/state/store.ts": "import",
    "apps/claude/ClaudeChat.tsx": "signal",
    "apps/claude/feature-flag.ts": "signal",
    "apps/claude/protocol/run-controller.ts": "tab",
    "apps/claude/ui/debug-sent.ts": "dev",
    "apps/claude/ui/useRecentTasks.ts": "tab",
    "apps/explorer/lib/fs-clipboard.ts": "tab",
    "apps/marketplace/install-flow.tsx": "import",
    # ---- lan (phone UI; per-device origin by nature, SPEC §52 BS-5) -----------
    "lan/App.tsx": "display",
    "lan/bots/hooks.ts": "cache",
}

_CATEGORIES = {"signal", "cache", "tab", "display", "import", "dev"}
# A storage API CALL (or a feature probe), not the bare word: a prompt string
# that tells an app author "never keep state in localStorage" must not trip it.
_STORAGE = re.compile(
    r"\b(localStorage|sessionStorage)\s*\.\s*(getItem|setItem|removeItem|clear|key|length)\b"
    r"|\b(localStorage|sessionStorage)\s*\["
    r"|typeof\s+(localStorage|sessionStorage)\b"
    r"|(globalThis|window)\s*\.\s*(localStorage|sessionStorage)\b"
)


def _code_only(src: str) -> str:
    """`src` with every comment removed and every string literal kept.

    A scanner rather than a regex, because the one wrong answer that matters
    is a `//` INSIDE a string (`"https://…"`): splitting the line there would
    hide a storage call written after the URL, and a hidden call is exactly
    what this suite exists to catch. Strings are kept verbatim (a key literal
    is what the moved-key audit looks for); template literals are treated as
    one string, their `${}` holes included — a storage call inside a template
    hole is not a pattern this codebase has, and treating it as text only
    makes that one case stricter to add later, not looser now."""
    out = []
    i, n = 0, len(src)
    quote = None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            quote = c
            out.append(c)
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _sources():
    for dirpath, _dirs, files in os.walk(_SRC):
        for name in files:
            if not name.endswith((".ts", ".tsx")):
                continue
            if ".test." in name or name.endswith(".d.ts"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, _SRC).replace(os.sep, "/")
            with open(full, encoding="utf-8") as f:
                yield rel, f.read()


def test_every_storage_use_is_listed_with_a_reason():
    offenders = sorted(
        rel for rel, src in _sources()
        if _STORAGE.search(_code_only(src)) and rel not in ALLOWED
    )
    assert offenders == [], (
        "These files touch localStorage/sessionStorage in code but are not in "
        "ALLOWED. A fact the user would miss across a relaunch belongs behind an "
        "API (D1386); a signal/cache/tab/display/import use goes in the list "
        f"with its category: {offenders}"
    )


def test_the_allowlist_names_only_files_that_still_use_storage():
    live = {rel for rel, src in _sources() if _STORAGE.search(_code_only(src))}
    stale = sorted(rel for rel in ALLOWED if rel not in live)
    assert stale == [], f"ALLOWED entries whose file no longer touches storage (remove them): {stale}"


def test_every_allowlist_entry_has_a_known_category():
    bad = {rel: cat for rel, cat in ALLOWED.items() if cat not in _CATEGORIES}
    assert bad == {}, bad


def test_the_functional_keys_are_gone_from_the_shell():
    """The group-A keys the 2026-10-18 audit moved. Each may survive ONLY as an
    import-only read (a `removeItem` of the same key must sit in the same file),
    never as a write (`setItem`) of a fact."""
    moved = [
        "fused-render:current-apps-order:v2",
        "fused-render:tasks-seen",
        "fused-render:notifications-seen",
        "fused-render:repo-updates-dismissed",
        "fused-render:attention-dismissed",
        "browser-bot.seen",
        "browser-bot.autostage",
        "browser-bot.sounds",
        "browser-bot.clear",
        "fused-render:scheduled-view-tabs",
        "fused-render:marketplace:python-ack",
        "fused-render.claude-health.dismissed",
        # the durable layout subset (ui_layout.json)
        "fused-render:sidebar",
        "fused-render:bookmarks-collapsed",
        "fused-render:current-apps-collapsed",
        "fused-render:explorer-side-hidden",
        "fused-render:explorer-side-width",
        "fused-render:explorer-side-maximized",
        "fused-render:viewstate",
        "fused-render:scheduled-view",
        "fused-render:scheduled-cal-range",
        "fused-render:scheduled-board-lanes",
        "fused-render:recent-paths",
    ]
    sources = list(_sources())
    problems = []
    for key in moved:
        naming = [rel for rel, src in sources if key in _code_only(src)]
        if not naming:
            continue
        # The key may be a constant in one file (`SEEN_KEY` in unread.ts) and
        # removed in another (the store that imports it); what must hold is
        # that SOMEONE removes it and nobody has grown a third use.
        by_rel = dict(sources)
        removers = [rel for rel in naming if "removeItem" in _code_only(by_rel[rel])]
        if not removers:
            # ...or the literal is an exported constant and the importer removes
            # it by name (`removeItem(SEEN_KEY)` in the bots store).
            for rel in naming:
                for const in re.findall(r"export const (\w+)\s*=\s*[\"']" + re.escape(key),
                                        _code_only(by_rel[rel])):
                    # ...directly or through a tiny helper (`dropKey(SEEN_KEY)`):
                    # the file must both name the constant and call removeItem.
                    removers += [r for r, src in sources
                                 if re.search(r"\b" + const + r"\b", _code_only(src))
                                 and "removeItem" in _code_only(src)]
        if not removers:
            problems.append(f"{key!r} named in {naming} but never removed (not an import)")
        if len(naming) > 2:
            problems.append(f"{key!r} named in {len(naming)} files: {naming}")
        # ...and nobody WRITES it: an import reads and removes. A `setItem` of
        # the literal, or of a constant that holds it, is the key coming back
        # as a browser-held fact — the thing this test exists to refuse.
        consts = set()
        for rel in naming:
            consts.update(re.findall(r"const (\w+)\s*=\s*[\"']" + re.escape(key) + r"[\"']",
                                     _code_only(by_rel[rel])))
        alts = [r"[\"']" + re.escape(key) + r"[\"']"] + [r"\b" + c + r"\b" for c in consts]
        writer = re.compile(r"setItem\(\s*(?:" + "|".join(alts) + ")")
        for rel, src in sources:
            if writer.search(_code_only(src)):
                problems.append(f"{rel}: writes {key!r} (setItem) — a moved key may only be read and removed")
    assert problems == [], problems
