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
//   CLOSING is a control ON the column — the chevron below, first thing in the
//   header, sitting on the seam the column collapses toward and pointing the way
//   it goes.
//   OPENING cannot live there, because a closed column hosts nothing, so that
//   half is the mode-icon button in the host's own bar (the crumb bar for a file,
//   the listing's search row for a folder), rendered ONLY while the column is
//   shut.
//
// Both at once is what the file sidebar's middle version did, and it was the
// wrong answer: the bar's toggle and the column's chevron sat a few pixels apart
// across the divider, two buttons for one piece of state, which reads as a
// rendering fault rather than as a choice. Exactly one of them is on screen at
// any moment, and each sits where its own action makes sense.
import type { ReactNode } from "react";
import { templateModeIcon } from "@apps/explorer/ModeSwitcher";
import type { PaneSide, PaneSideEntries } from "@apps/explorer/listing/pane-side";

function CloseChevron() {
  return (
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
      <polyline points="9 6 15 12 9 18" />
    </svg>
  );
}

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
      <CloseChevron />
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
// one above. Null entries never reach here — a mode with no entry is not offered
// (pane-side's paneSideList) — but the fallback is the placeholder box
// templateModeIcon draws for an icon-less template rather than a hole in the bar.
export function paneSideIcon(side: PaneSide, entries: PaneSideEntries): ReactNode {
  if (side === "preview") return PREVIEW_SIDE_ICON;
  const entry = side === "claude" ? entries.claude : entries.git;
  return entry ? templateModeIcon(entry) : PREVIEW_SIDE_ICON;
}
