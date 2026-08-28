// The chrome a RIGHT-HAND COMPANION COLUMN carries, shared by the two surfaces
// that have one: the file preview's sidebar (PreviewSidebar, over a single file)
// and the folder listing's preview pane (ListingPreviewPane, over a directory).
//
// The two arrived at the same layout from opposite ends and are now one thing
// stated once — a header strip whose left end is the way OUT of the column and
// whose right end is the mode control, over a full-height frame beside a
// draggable seam. Keeping the buttons here rather than one copy each is what
// makes "the folder pane matches the file sidebar" true rather than intended.
//
// ONE AFFORDANCE, TWO PLACES, CHOSEN BY STATE — the rule the listing pane's own
// visibility control already followed (SPEC FS-10):
//
//   CLOSING is a control ON the column — the panel glyph below, first thing in
//   the header, sitting on the seam the column collapses toward and naming the
//   side it is on (platform/ui/PanelIcon).
//   OPENING cannot live there, because a closed column hosts nothing, so that
//   half is the mode-icon button in the host's own bar (the crumb bar for a file,
//   the listing's search row for a folder), rendered ONLY while the column is
//   shut.
//
// Both at once is what the file sidebar's middle version did, and it was the
// wrong answer: the bar's toggle and the column's own close sat a few pixels apart
// across the divider, two buttons for one piece of state, which reads as a
// rendering fault rather than as a choice. Exactly one of them is on screen at
// any moment, and each sits where its own action makes sense.
import { useRef, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import PanelIcon from "@platform/ui/PanelIcon";
import { reopenWidth } from "@platform/lib/panel-drag";
import { templateModeIcon } from "@apps/explorer/ModeSwitcher";
import { CONTENT_MIN_W, MIN_W } from "@apps/explorer/lib/side-width";
import { setSideWidth } from "@apps/explorer/lib/side-store";
import {
  paneSideIconEntry,
  type PaneSide,
  type PaneSideEntries,
} from "@apps/explorer/listing/pane-side";

// `what` names the panel in both labels — the mode's display name ("Claude",
// "Git", "Preview"), so the tooltip says which panel rather than "the panel".
export function SideCloseButton({ what, onClick }: { what: string; onClick: () => void }) {
  const label = "Hide the " + what + " panel";
  return (
    <button
      type="button"
      className="bar-ctl bar-ctl-icon side-close"
      title={label}
      aria-label={label}
      onClick={onClick}
    >
      {/* A frame with its RIGHT half filled — this column, on this side. The
          same glyph mirrored is what the global sidebar's toggle wears
          (platform/ui/PanelIcon), which is what makes the two edges of the
          window read as one idea rather than as two chevrons pointing
          opposite ways for unrelated reasons. */}
      <PanelIcon side="right" />
    </button>
  );
}

// The opener, and it wears the COMPANION'S OWN ICON rather than a chevron: a
// chevron only ever said "a panel goes here", while the icon says WHICH panel, so
// the button announces what the click will get you with no hover needed. Which
// icon that is, is the caller's business — the mode it would reopen is the one
// last open on that surface.
export function SideToggleButton({
  what,
  icon,
  onClick,
}: {
  what: string;
  icon: ReactNode;
  onClick: () => void;
}) {
  const label = "Show the " + what + " panel";
  return (
    <button
      type="button"
      className="bar-ctl bar-ctl-icon side-toggle"
      title={label}
      aria-label={label}
      aria-expanded={false}
      onClick={onClick}
    >
      <span className="mode-menu-icon">{icon}</span>
    </button>
  );
}

// THE SEAM A SHUT COLUMN LEAVES BEHIND — the other half of `SideCloseButton`,
// for hands rather than for eyes. `SideToggleButton` above is still the visible
// opener and still the one anybody finds first; this is the gesture that answers
// the one it costs nothing to try: having just dragged the column away past its
// floor, drag it back.
//
// It is NOT a second control. A control announces itself, occupies space, and
// has to be found; this announces nothing, occupies no layout at all (a
// zero-width flex item whose ::before hangs a 6px hit strip off the split's right
// edge — preview.css), and is only ever discovered by someone already reaching
// for the edge they last saw it at. That is the whole reason the "one affordance,
// two places" rule at the top of this file is not violated by its existence:
// there is still exactly one BUTTON for this state.
//
// It starts BELOW the crumb bar (`top: var(--topbar-h)`) on purpose. The bar's
// own trailing controls — the mode switcher, and the opener itself — sit at that
// same right edge, and a strip over the top of them would eat the outer few
// pixels of their hit area to serve a gesture nobody has started yet. The seam
// belongs beside the CONTENT, which is what it resizes.
export function SideReopenEdge({ onOpen }: { onOpen: () => void }) {
  const ref = useRef<HTMLDivElement>(null);

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault(); // no text selection before the html.side-reopening rule lands
    const split = ref.current?.closest<HTMLElement>(".stat-split");
    if (!split) return;

    // CAPTURE ON documentElement, NOT ON THIS STRIP, and the difference is the
    // whole gesture. The moment the pull crosses its threshold this component
    // unmounts — the column it just opened has taken its place — and capture on
    // a detached element is capture lost, which would end the drag exactly when
    // it starts being about a width. The same reason a row drag captures here
    // (listing/row-drag.ts). It also buys what the divider's capture buys:
    // surviving the pointer crossing into either iframe.
    const root = document.documentElement;
    try {
      root.setPointerCapture(e.pointerId);
    } catch {
      /* no capture; the listeners below still see the gesture */
    }
    // Cursor, text selection, and inert iframes for the drag's duration. On the
    // root rather than driven off the strip's own class, for the same reason the
    // capture is: the strip is not going to be here for the second half.
    root.classList.add("side-reopening");

    let opened = false;
    const onMove = (ev: PointerEvent) => {
      if (ev.pointerId !== e.pointerId) return;
      const rect = split.getBoundingClientRect();
      const max = rect.width - CONTENT_MIN_W;
      if (max < MIN_W) return; // container too narrow to express a split
      // A right-hand panel's implied width grows as the cursor moves LEFT. The
      // shut column occupies nothing, so the pull is measured from the split's
      // own right edge — hence a `closedWidth` of 0, where the global sidebar
      // passes the width of the rail it leaves behind.
      const w = reopenWidth(rect.right - ev.clientX, 0, MIN_W, max);
      if (w === null) return; // still short of OPEN_PULL — nothing has happened yet
      // WIDTH BEFORE OPEN, and the order is load-bearing on the first pass:
      // PreviewSidebar seeds its width from this store as it mounts, so writing
      // second would open the column at the remembered width and only then jump
      // it to the dragged one. On every pass after, the store is how the width
      // reaches the already-mounted column at all (lib/side-store).
      setSideWidth(w);
      if (!opened) {
        opened = true;
        onOpen();
      }
    };
    const onUp = (ev: PointerEvent) => {
      if (ev.pointerId !== e.pointerId) return;
      root.classList.remove("side-reopening");
      try {
        root.releasePointerCapture(e.pointerId);
      } catch {
        /* never captured */
      }
      root.removeEventListener("pointermove", onMove);
      root.removeEventListener("pointerup", onUp);
      root.removeEventListener("pointercancel", onUp);
    };
    root.addEventListener("pointermove", onMove);
    root.addEventListener("pointerup", onUp);
    root.addEventListener("pointercancel", onUp);
  };

  return (
    <div
      ref={ref}
      className="preview-side-reopen"
      role="separator"
      aria-orientation="vertical"
      aria-label="Drag to show the panel"
      onPointerDown={onPointerDown}
    />
  );
}

// The listing pane's "Preview" glyph. The other two panes wear their template's
// own icon.svg, but `preview` is not a template — it is "whatever this row's
// default view is" — so the shell bakes one, in the same 16px currentColor stroke
// as every other glyph in these bars. A play button in a frame: render the row.
const PREVIEW_SIDE_ICON = (
  <svg
    viewBox="0 0 24 24"
    width="16"
    height="16"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <rect x="3" y="3" width="18" height="18" rx="3" />
    <path d="M10 8.75 15.5 12 10 15.25Z" />
  </svg>
);

// Icon for one of the folder pane's three modes. `claude` and `git` are real
// templates borrowed from the folder (lib/dir-mode), so they get their registry
// icon exactly as they do on every other mode surface; `preview` gets the baked
// one above.
//
// An UNOFFERED mode is now an ordinary case rather than an impossible one: the
// switcher lists all three and disables the ones the folder cannot show
// (pane-side's paneSideMenu), so a folder outside a repository draws a Git row
// with no framable entry behind it. It still wears the GIT ICON — a disabled row
// is the mode with the click taken away, and the glyph is the last thing that may
// change — which is what `paneSideIconEntry` is for: the offered entry, else the
// binding the stat reported before the gate refused it (lib/dir-mode's `bound`).
//
// Only a mode bound NOWHERE falls through to templateModeIcon's letter box, since
// then no real glyph exists to draw. It must never fall back to the PREVIEW
// glyph, which this briefly did: two rows wearing one icon in a three-row menu
// reads as a duplicate entry rather than as an unavailable one.
export function paneSideIcon(side: PaneSide, entries: PaneSideEntries): ReactNode {
  if (side === "preview") return PREVIEW_SIDE_ICON;
  const entry = paneSideIconEntry(side, entries);
  return templateModeIcon(entry ?? { mode: side, path: null, icon: null });
}
