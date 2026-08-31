// THE status-bar indicator, shared by every chip (D590, user: "lets just stick
// to a circle for all items"). Outlined when the section holds nothing, filled
// when it holds something — the section's whole state, since no chip carries a
// count any more (D588).
//
// A COMPONENT RATHER THAN FOUR COPIES OF THE MARKUP. Four chips wanting
// identical geometry is exactly the point at which copies start to drift:
// D588's centring bug existed because the circle's wrapper differed from what
// the CSS assumed, and a per-chip `<span>` in four files is four chances to
// reintroduce that. One element, one class pair, one place to change.
//
// IN PLATFORM because `check-boundaries.mjs` forbids platform importing shell,
// and the chips straddle that line — `DownloadManager.tsx` is platform while
// `ActivityDock.tsx` and `RepoUpdatesDock.tsx` are shell.
//
// MUST STAY A DIRECT FLEX CHILD of `.dl-toggle`, which is what centres it:
// that element is already `display: flex; align-items: center`, so the circle
// centres on the chip's own box with no `vertical-align` involved (D588 — the
// old nesting inside `.dl-summary` leaned on `vertical-align: middle`, which
// aligns to the baseline plus half the parent's x-height and sits visibly
// low). Rendering this anywhere other than immediately inside the toggle
// reintroduces that bug, so callers place it as `.dl-summary`'s sibling.
//
// ANNOUNCED IN WORDS, not hidden and not numbered. The circle is the only
// state a sighted user gets, so hiding it would make the chip's state
// sight-only — but the accessible name must not smuggle a count back in
// either (user: "no count. just a circle outlined or filled"). So the caller
// passes a worded label ("no jobs" / "jobs running") and `role="img"` makes it
// announce as a single thing rather than as an empty span.
export default function StatusDot({ on, label }: { on: boolean; label: string }) {
  return (
    <span className={"dl-dot" + (on ? " is-on" : "")} role="img" aria-label={label} />
  );
}
