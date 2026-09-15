// THE NARROWEST A RIGHT-HAND CHAT PANE MAY BE, stated once for the two panes
// that are the same chat in the same shell: the Explorer listing's Claude pane
// (`.listing-pane-slot`) and the Tasks page's side peek.
//
// ── WHY IT IS A MEASUREMENT AND NOT A TASTE ─────────────────────────────────
//
// A chat pane is only a chat pane while you can still SEND from it. The
// composer's control row — model · effort · permission · calendar · send — is
// the widest fixed thing in the panel, and below its natural width it starts
// folding: the pills tighten (`.c-composer-row.is-tight`), then the row breaks
// and the send button drops to a line of its own (`.is-stack`). A panel that
// opens already in that state is a panel whose first impression is a broken
// toolbar, so the floor is the width at which the row still sits on one line
// with its ordinary spacing (Akshil, 2026-09-14 — design.md, Polish batch 3).
//
// ── HOW THE NUMBER WAS ARRIVED AT ───────────────────────────────────────────
//
// Derived from `apps/claude/styles/composer.css` rather than measured in a
// browser (this round had no browser to measure in, which is a real caveat:
// the figure is arithmetic over declared paddings and an average glyph advance,
// not a rect).
//
//   three pills   `.c-pill` is `padding: 3px 8px` inside a 1px border, and a
//                 select carries ~20px more for its caret — so "Sonnet 4.5",
//                 "Medium" and "Ask" come to 98 + 74 + 56
//   calendar      28 · send `.c-send` 32 (a 32px circle)
//   gaps          four at `gap: 6px`
//   composer      `.c-composer` adds `padding: … 14px` and a 1px border
//   pane          and the pane its own ~12px either side
//
// which lands at 366 for the SHORTEST plausible option strings. The option text
// is the one part that grows — "Accept edits", "Opus 4.1", a long model name —
// and none of it is this module's to control, so the floor carries headroom for
// it and comes out at 400. Akshil's own estimate was 380-420; this sits in it.
//
// It replaces 220, which was the Explorer pane's own number and predates the
// composer row having three selects in it.
export const SIDE_PANE_MIN_WIDTH = 400;

/** The same number for the stylesheets, which cannot import this one. Written
 *  on `:root` in styles/tokens.css; `pane-metrics.test.ts` pins the two
 *  together, so neither can move without the other. */
export const SIDE_PANE_MIN_VAR = "--side-pane-min";
