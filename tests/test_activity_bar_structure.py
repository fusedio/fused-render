"""Structural rules for the status bar's three sections that have nothing to do
with any one section's content — placement, persistence, and always-present-ness.

These used to sit inside `tests/test_queue_dock.py` alongside checks against a
now-deleted queue card; they outlived that file's split (D661's queue removal)
because they were never about the queue at all. They read source text, like the
template suites, rather than rendering anything — the real BEHAVIOUR the bar's
composition produces is `frontend/src/platform/ui/StatusBar.test.tsx`'s job
(see `test_the_bar_reserves_space_inside_main_not_the_floating_column` below);
these stay structural/placement checks only.
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONT = os.path.join(_ROOT, "frontend", "src")
# `QueueDock.tsx` and `EnginesDock.tsx` were folded into `shell/ActivityDock.tsx`
# by the status-bar Activity-chip merge — `dock` below reads that file.
_DOCK = os.path.join(_FRONT, "shell", "ActivityDock.tsx")
_CARD = os.path.join(_FRONT, "platform", "ui", "DownloadManager.tsx")
_HOST = os.path.join(_FRONT, "platform", "ui", "NotificationHost.tsx")
_BAR = os.path.join(_FRONT, "platform", "ui", "StatusBar.tsx")
_APP = os.path.join(_FRONT, "shell", "App.tsx")
_CSS = os.path.join(_FRONT, "styles", "notifications.css")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_the_fold_is_never_persisted_at_all():
    """D603 SUPERSEDES this test's original subject. It used to police WHO may
    write the stored fold — the D567 guard, which survived D574's auto-open,
    D580's auto-close and D582's exclusivity, each of which had to be kept away
    from `localStorage`. There is no longer a stored fold to write: the key,
    `loadCollapsed` and `saveCollapsed` are deleted outright (user: "on page
    reload the models popover auto opens for some reason" — not the auto-open
    path at all, but a stored `"0"` from an earlier click being faithfully
    restored on every load since).

    So the invariant gets STRONGER rather than disappearing: no source file in
    this feature may touch `localStorage` for the fold at all. Deleting the
    writer is what makes that checkable as an absence instead of as a count.

    Checked on CODE, not on comments — several of these files legitimately
    explain in prose why the persistence was removed, and a test that tripped
    over its own explanation would be worse than no test.
    """
    def code_only(src):
        return "\n".join(
            line for line in src.splitlines()
            if not line.strip().startswith(("//", "*", "/*")))

    card = _read(_CARD)
    dock = _read(_DOCK)
    for label, src in (("the card", card), ("the dock", dock)):
        stripped = code_only(src)
        assert "localStorage" not in stripped, f"{label} must not persist the fold"
        assert "saveCollapsed" not in stripped, f"{label} must not write the fold"
        assert "loadCollapsed" not in stripped, f"{label} must not read a stored fold"

    for module in ("autoExpand.ts", "exclusiveSection.ts"):
        stripped = code_only(_read(os.path.join(_FRONT, "platform", "lib", module)))
        assert "localStorage" not in stripped, f"{module} must not persist anything"

    # And every section, not just this card's: four separate keys existed
    # (`models-`, `jobs-`, `repo-updates-`, `engines-collapsed`) and all four go.
    # `EnginesDock.tsx` (and `QueueDock.tsx`, covered above as `dock`) no longer
    # exist as separate files — both were folded into `ActivityDock.tsx` by the
    # status-bar merge, so it stands in for their old checks here.
    for rel in (("shell", "ModelsDock.tsx"), ("shell", "RepoUpdatesDock.tsx"),
                ("shell", "ActivityDock.tsx")):
        stripped = code_only(_read(os.path.join(_FRONT, *rel)))
        assert "localStorage" not in stripped, f"{rel[-1]} must not persist the fold"
        assert "-collapsed" not in stripped, f"{rel[-1]} must not keep a fold key"

    # The one remaining setter is the chip's own click. `close` is `forceClose`
    # (transient only), which is what D585 finding 2 fixed and D603 does not undo.
    assert card.count("setCollapsed(") == 1, "the chip's own click, and nothing else"


def test_the_column_owns_where_it_sits():
    """Placement belongs to StatusBar / notifications.css (D563) — neither the
    Activity dock nor the card positions itself inline, exactly as it never did
    when NotificationHost owned this instead."""
    dock = _read(_DOCK)
    card = _read(_CARD)
    for gone in ("position: fixed", "position:fixed", "zIndex", "z-index"):
        assert gone not in dock
        assert gone not in card


def test_the_bar_reserves_space_inside_main_not_the_floating_column():
    """D563 (user call: "the collapsed notification is also taking too much
    space... it is impossible to use the claude template with it"). The two
    cards moved OUT of NotificationHost's fixed, floating column and into a
    bar mounted inside `#main`, which reserves layout space for it instead of
    overlaying whatever is under it. Toasts, FdaCard and ServerStatusBanner —
    all short-lived or exceptional enough that overlaying the page is still
    the right call — are the ones left in NotificationHost's column.

    The real BEHAVIOUR this bar composition produces — three sections in
    order, an omitted slot rendering nothing rather than an empty wrapper —
    is a `frontend/src/platform/ui/StatusBar.test.tsx` render test now
    (code review finding #8: a source-literal grep here cannot see whether
    a component actually behaves the way its source claims). This function
    stays a structural/placement check only."""
    app = _read(_APP)
    host = _read(_HOST)
    bar = _read(_BAR)
    # StatusBar is mounted INSIDE #main, alongside the routed content, not as
    # a sibling of it the way NotificationHost is.
    main_at = app.index('<div id="main">')
    bar_use_at = app.index("<StatusBar")
    main_close_at = app.index("</div>", bar_use_at)
    assert main_at < bar_use_at < main_close_at, "StatusBar must render INSIDE #main"
    assert "<NotificationHost />" in app, "the two moved entries are gone from its props"
    assert "activity?: ReactNode" not in host, "NotificationHost no longer takes them"
    assert "repoUpdates?: ReactNode" not in host
    assert "DownloadManager" not in host, "the bare-manager fallback moved to StatusBar"
    assert "activity?: ReactNode" in bar
    assert "repoUpdates?: ReactNode" in bar
    assert "models?: ReactNode" in bar, "D565: a third, always-present section"


def test_the_bar_is_always_present_now_not_gone_when_empty():
    """D565 (user verdict on the shipped round-1 bar: "this is very ugly.
    different categories of status bar should be always present and look
    better"). Round 1's `.status-bar:empty { display: none }` rule —
    collapsing the bar to nothing the moment both cards had nothing to show
    — is SUPERSEDED, not extended: the three categories are a fixed status
    readout now, each drawing its own idle text (`No models loaded` /
    `No activity` / `No updates`, D569) instead of vanishing. `#main` is
    therefore permanently shorter by the bar's height on every page, which
    is the accepted cost the user's own words call for."""
    css = _read(_CSS)
    card = _read(_CARD)
    assert ".status-bar:empty" not in css, "the always-gone rule must not survive next to always-present"
    # D573: idle text moved from the chip into the panel it opens. The chip is
    # "Activity" now (status-bar merge), so its idle sentence names that.
    assert '<div className="dl-panel-empty">No activity</div>' in card, "the activity section's own idle text"
