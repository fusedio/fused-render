// The preview pane's width arithmetic, as pure functions: the split threshold,
// the undragged breakpoints, the pixel clamps, and the divider drag's
// px→fraction step. The stateful half — the hook and the drag — lives in
// pane.ts.
//
// There is no parse of a stored width here, and there was: a dragged fraction
// used to be serialised into the per-folder viewstate and read back, so it
// needed validating on the way in (legacy pixel values, a whole-container 1).
// Nothing is stored per-folder any more — see the next paragraph for what is.
//
// **THE STORED WIDTH IS PIXELS, SHARED WITH THE FILE SIDEBAR** (D443,
// `lib/side-store.ts`) — a change from this pane's own history, where a
// dragged FRACTION lived in `listing/pane-store.ts` (now deleted) independently
// of the file view's pixel-based sidebar. `paneFracFromSharedWidth` below is
// the seam: it takes the shared pixel number and this pane's OWN floors and
// share ceiling (`clampSharedPaneWidth` — the floors narrower than the file
// sidebar's, since a listing column tolerates less room than a chat
// composer) and answers the fraction THIS container should render, so the
// two surfaces can share one stored number while keeping different floors.
// `dragPaneFrac` reads back through the exact same clamp, so a LOCAL drag on
// this pane and an imported width are bounded identically — see
// `MAX_PANE_SHARE`'s own comment for why that turned out to be the fix rather
// than capping only the imported path. Everywhere else in this module, width
// is still a FRACTION of the split container: that is what keeps the pane
// proportional when the window resizes (see pane.ts), and pixels appear only
// as the floors, the shared stored value, and the duration of a drag.
//
// It is a separate module for a testability reason, not a tidiness one:
// pane.ts imports @platform/lib/router, which reads `location` at MODULE INIT
// (its embed-prefix constant), so merely importing pane.ts in a DOM-free bun
// test throws. Splitting the math out is the cleaner of the two fixes —
// deferring the router's read would make a genuinely load-time constant lazy
// for every caller in the app to suit a test, whereas this arithmetic never
// wanted the router in the first place.
import { companionFrac } from "@apps/explorer/lib/side-width";

const PANE_MIN_W = 220;
const LIST_MIN_W = 60;

// **THE PANE'S WIDTH: the companion share of its container** — 30%, or 50% at
// 1000px and under (D283 amending D282). The same function the file view's sidebar
// uses, imported from there so the two cannot drift apart again; the reasoning,
// including why D282's argument for deleting the small step was wrong, is on
// `companionFrac`.
//
// It is a function of the CONTAINER's width and of nothing else — never the
// viewport, so an embedded pane in a small frame gets the same 50% a small window
// does.
//
// Three pieces of responsive machinery are DELETED here, on the owner's
// instruction ("remove any complicated breakpoint logic"):
//
//   * the 30/50/70 TIERS (`defaultPaneFrac`), stepping on 1000px and 1440px
//     container breakpoints, with a `220/containerW` floor folded in;
//   * `PANE_SPLIT_MIN_W` / `shouldShowPane` — the **700px gate** that decided
//     whether there was a pane AT ALL;
//   * `useSplitIsWide` in pane.ts, the gate's second consumer (Preview's browse
//     chip), which asked the measurement for a verdict nothing needs now.
//
// **The container MEASUREMENT is back, and only it** (D283): `useSplitWidth` returns
// so the small step above can be asked of the container rather than of the window.
// One boolean's worth of responsiveness — small or not — where there used to be a
// visibility gate and three tiers reading the same number. The pane's PRESENCE is
// still a property of WHICH Listing this is (`paneEnabled` in
// Listing.tsx: not embedded, not a snapshot, not a panel pane) — a question about
// the surface, not about how many pixels it happens to have. The pixel FLOORS
// below stay: they are clamps a drag and the CSS must agree on, not conditions on
// the layout, and they are the only reason a 30% pane on a very narrow window is
// still a usable column.
// Re-exported under its OWN name, deliberately not as `defaultPaneFrac`: that name
// belonged to the tier ladder, a test pins its absence, and reviving the identifier
// for a two-value step would make the ladder look like it came back.
export {
  COMPANION_FRAC as PANE_DEFAULT_FRAC,
  companionFrac,
} from "@apps/explorer/lib/side-width";

// The one place the pixel clamps live, so the drag cannot disagree with the
// CSS floors: the pane keeps at least PANE_MIN_W, and the list keeps at least
// LIST_MIN_W (a sliver — the columns shed themselves via container queries as
// it narrows). PANE_MIN_W is applied last: in the degenerate case (a container
// too small for both minimums) the pane keeps its floor and the list scrolls.
// CSS mirrors both floors (.listing-pane-slot / .listing-main min-width),
// which is what holds them on a window resize — the stored fraction is
// deliberately proportional and knows nothing about pixels.
export function clampPaneWidth(containerW: number, width: number): number {
  return Math.max(PANE_MIN_W, Math.min(containerW - LIST_MIN_W, width));
}

// THE CEILING ON AN IMPORTED WIDTH — AND, SINCE THE SECOND REVIEW PASS, ON A
// LOCAL DRAG TOO. `clampPaneWidth` alone bounds a width in PIXELS (never
// below PANE_MIN_W, never so wide the list loses LIST_MIN_W of ITS OWN
// container), which was believed enough for a drag performed ON this
// container, where the cursor is visibly the thing choosing the number. It
// is not: the pixel number can now arrive from SOMEWHERE ELSE too (the file
// sidebar's own drag, D443) — the file sidebar's ceiling is `containerW -
// CONTENT_MIN_W` on whatever monitor it was dragged on, which can be a much
// bigger pixel count than this container has ever seen, and reading it back
// through the pixel floors alone would open this listing at its 60px sliver
// — or, across an ordinary window resize (this container shrinking under a
// WIDTH that no longer moves with it, since the stored number is pixels and
// not a proportion), squeeze it there without any drag at all.
//
// A SHARE ceiling bounds the fraction directly, on top of the pixel floors,
// so a number chosen for a different container — or for this one, before it
// shrank — can never starve the list beyond what the design ever intends a
// companion column to take. **It applies uniformly, to `dragPaneFrac` and
// `paneFracFromSharedWidth` alike, through the one function both now call
// (`clampSharedPaneWidth` below)** — the alternative, capping only the
// IMPORTED path and leaving a local drag free to reach the old ~94% pixel-
// floor ceiling, was rejected: the render already re-derives its fraction
// from the STORED pixel number on every commit (`pane.ts`), so a local drag
// that reached past the cap while dragging and an import that started there
// would be indistinguishable one render later — "was this number dragged
// here or somewhere else" is not a fact this module (or the shared store) can
// answer, and a value that is capped when read back but not when written is
// exactly the mismatch the second review pass caught: the divider detaching
// from the cursor mid-drag, and a stored width the very next render disagreed
// with.
export const MAX_PANE_SHARE = 0.7;

// THE FLOOR-LAST CLAMP, shared by every caller that turns a pixel number —
// wherever it came from — into a width THIS container can render. Two rules,
// applied in this order and no other:
//
//   1. never wider than the smaller of the list's floor (`containerW -
//      LIST_MIN_W`) and the share ceiling (`containerW * MAX_PANE_SHARE`);
//   2. never narrower than `PANE_MIN_W`, EVEN IF that disagrees with rule 1.
//
// Floor-last is not a stylistic choice: below ~314px (`PANE_MIN_W /
// MAX_PANE_SHARE`) the share ceiling alone would ask for fewer pixels than
// the pane's own floor — 196px of a 280px container, for instance — and
// `.listing-pane-slot`'s CSS `min-width: 220px` would then override the
// computed flex-basis, so the rendered layout and the fraction this function
// answered would disagree. Applying the floor SECOND, unconditionally, is
// what `clampPaneWidth` already did for the pixel floors alone; this keeps
// that guarantee once a share ceiling is in the mix too. In that narrow band
// the floor simply wins outright — the pane is wider than `MAX_PANE_SHARE`
// of its container there, exactly as it always was before the cap existed,
// because there is no other number the container can honestly render.
export function clampSharedPaneWidth(containerW: number, px: number): number {
  const upper = Math.min(containerW - LIST_MIN_W, containerW * MAX_PANE_SHARE);
  return Math.max(PANE_MIN_W, Math.min(upper, px));
}

// THE SHARED-WIDTH SEAM (D443): the fraction THIS container should render,
// given the pixel width dragged either here or on the file sidebar
// (`lib/side-store.ts` holds one number for both) and this container's own
// measured width. `sharedPx: null` (nothing dragged yet, in either surface,
// this session) answers the plain companion share.
//
// The shared number is clamped into THIS pane's OWN floors AND share ceiling
// before it is turned into a fraction — never the file sidebar's wider
// floor — which is what lets the two surfaces share one stored pixel value
// while keeping different minimums: a width dragged comfortable for a chat
// composer (the file sidebar's 380px floor) is still valid input here, just
// re-clamped against this pane's narrower 220px one, and vice versa.
//
// A DEGENERATE CONTAINER (under PANE_MIN_W + LIST_MIN_W = 280px — the same
// threshold `dragPaneFrac` guards) answers the plain companion share too,
// UNCONDITIONALLY, before the shared number is even consulted: at that width
// `clampSharedPaneWidth` returns PANE_MIN_W regardless of input, which is
// more pixels than the container has, and dividing it out yields a fraction
// over 1 (`flexBasis: "110%"`) — a number CSS cannot render sanely and that
// `dragPaneFrac` itself refuses to produce (it answers `null` there, and the
// caller changes nothing). This module has no `null` to hand back — a
// fraction always renders — so it hands back the one number that was always
// the answer for an unmeasurable or unsplittable container anyway.
//
// An unmeasured container (0, NaN) takes the same early return —
// `companionFrac` already treats that as "not small" — rather than dividing
// by a width that cannot back a fraction.
export function paneFracFromSharedWidth(sharedPx: number | null, containerW: number): number {
  if (!(containerW >= PANE_MIN_W + LIST_MIN_W)) return companionFrac(containerW);
  if (sharedPx === null) return companionFrac(containerW);
  return clampSharedPaneWidth(containerW, sharedPx) / containerW;
}

// The divider drag, in one pure step: the cursor's distance from the
// container's right edge is the pane's wanted PIXEL width, clamped by the
// shared floors AND the share ceiling (`clampSharedPaneWidth`, same function
// `paneFracFromSharedWidth` reads back with — see the header on why a local
// drag is capped identically to an imported width) and divided back out
// into the fraction that is what actually gets stored and rendered.
//
// `null` means THIS CONTAINER CANNOT EXPRESS A SPLIT, and the caller must
// neither move the pane nor record anything. Two cases, one rule:
//   • no width at all (unmeasurable, zero-sized);
//   • narrower than both floors together (PANE_MIN_W + LIST_MIN_W = 280px — a
//     panel-split grid, a zoomed-in window). There the clamp returns
//     PANE_MIN_W whatever the cursor does, so the fraction it yields describes
//     the CONTAINER'S narrowness and not the user's choice — at 220px wide it
//     is exactly 1.0, "the pane takes everything", which no wider window can
//     honour: re-opening the folder on a normal screen left the list at its
//     60px sliver, permanently, from one drag in a narrow pane. A number that
//     is not a choice must not be recorded as one, and capping it just below
//     1 would still keep a proportion nobody picked.
// Above ~314px (`PANE_MIN_W / MAX_PANE_SHARE`) the ceiling is `MAX_PANE_SHARE`
// itself; below that and down to 280px the pane's own floor wins instead (see
// `clampSharedPaneWidth`) — either way the fraction can never reach 1.
export function dragPaneFrac(containerW: number, rawPx: number): number | null {
  if (!(containerW >= PANE_MIN_W + LIST_MIN_W)) return null;
  return clampSharedPaneWidth(containerW, rawPx) / containerW;
}

// WHETHER A DRAG HAS PULLED THE PANE SHUT — the listing pane's version of the
// sidebars' drag-to-close (platform/lib/panel-drag `resizeWidth` returning
// null). #680 gave the gesture to both SIDEBARS and left this pane holding at
// its floor — exactly the clamp-says-"this is as narrow as it goes" reading
// #680 existed to fix. Same grammar here: between the floor and half the floor
// the pane sticks (the clamp above already renders that resistance band), and
// only a pull clean through it — the cursor within PANE_MIN_W/2 of the
// container's right edge — reads as "shut it". Half the floor rather than a
// flat count, so the band scales with the floor exactly as `closeOverdrag`
// does for the sidebar. In a container too narrow to express a split there is
// no resistance band to pull through (dragPaneFrac is already null there), so
// there is no close either: a gesture whose warning cannot render must not act.
export function paneDragCloses(containerW: number, rawPx: number): boolean {
  if (!(containerW >= PANE_MIN_W + LIST_MIN_W)) return false;
  return rawPx < PANE_MIN_W / 2;
}
