// THE PANEL GLYPH — one icon, stated once, for every control whose job is
// "this panel goes away / comes back".
//
// IT REPLACES A CHEVRON. Every panel control in the app wore one (platform/ui/
// Chevron, which survives — the crumb bar's history arrows are a genuine ‹ ›
// and are now its only caller), and the argument that file makes for itself
// still holds: one spelling, one weight, no drift. What the chevron got wrong
// here was not its geometry but its MEANING. A bare ‹ or › says "something moves
// that way"; it is the glyph on a carousel arrow, a disclosure triangle and a
// menu caret, and in a window with a collapsible column on each side it said the
// same thing on both. Which panel, and whether it is currently open, were left
// entirely to the tooltip. Handing the glyph back to the one control that really
// does mean "go that way" is the other half of the same fix.
//
// A frame with one side filled says both without hovering: the OUTLINE is the
// window, the FILLED HALF is the panel, and which half it is names the column.
// It is the icon VS Code, Xcode and every editor with a sidebar converged on, so
// it arrives already learned. This is deliberately NOT a stateful glyph — the
// same icon draws the collapse control and the expand control for one panel,
// exactly as those editors do. A toggle that changes its picture makes the user
// read the picture to find out what state they are in; a toggle that keeps it
// lets them read the SCREEN, where the panel either is or is not.
//
// SIDE IS THE ONLY PROP, and it is a fact about which column the button belongs
// to, not a style choice: `left` for the global sidebar (SidebarFrame, both its
// collapse button and its rail's expand button — same panel, same glyph),
// `right` for the companion column beside a file preview or a folder listing
// (SideChrome). Size and weight are deliberately not props, for the reason the
// chevron file was written in the first place: three hand-rolled copies of one
// glyph had drifted to two different stroke weights, and a caller that wants a
// different weight wants a different icon.
//
// 16px on a 0 0 24 24 grid at strokeWidth 2 is the app's icon norm — the bar and
// rail glyphs, ModeSwitcher, SideChrome's baked preview glyph. The frame reuses
// that preview glyph's exact box (`rect x=3 y=3 w=18 h=18 rx=3`) so the two read
// as siblings when they sit in the same header strip.

// The filled half is a path rather than a second `<rect>` because it has to
// carry the frame's OWN rounded corners on its outer edge — a square-cornered
// rect tucked inside a 3-unit radius shows two slivers of background at the
// corners at 16px, which reads as a rendering fault. Each traces the frame's
// arc on the two corners it owns and runs straight down the split line:
// counterclockwise for the left half, clockwise for the right.
const FILL = {
  left: "M9 3H6a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3h3Z",
  right: "M15 3h3a3 3 0 0 1 3 3v12a3 3 0 0 1-3 3h-3Z",
} as const;

// The split, drawn as well as the fill and not instead of it: the fill alone
// would leave the panel edge at the mercy of whatever `currentColor` resolves
// to against the frame, and a stroked line keeps the division legible when the
// button is muted (the disabled/rest states these bars use are opacity-based).
const SPLIT = {
  left: "M9 3v18",
  right: "M15 3v18",
} as const;

export default function PanelIcon({ side }: { side: "left" | "right" }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="3" width="18" height="18" rx="3" />
      {/* `stroke="none"` explicitly: the filled half inherits the svg's stroke
          otherwise, and a 2-unit stroke around it bleeds a half-unit PAST the
          frame's own outline on three sides. */}
      <path d={FILL[side]} fill="currentColor" stroke="none" />
      <path d={SPLIT[side]} />
    </svg>
  );
}
