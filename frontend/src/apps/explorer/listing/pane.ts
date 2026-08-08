// The preview pane (right-hand split): per-folder visibility/width state and
// the usePreviewPane hook that owns the toggle + divider drag.
//
// Visibility follows the sort's model: an explicit `?preview` in the URL wins
// (and rides along on directory navigation — see lib/router navigate, which
// carries it so the pane is sticky between folders), otherwise this folder's
// saved viewstate (keys `pane`/`panew` alongside `sort`/`order`). Width is
// viewstate-only — one machine's split isn't something a shared link should
// impose. Default ON — no `preview` param and no saved viewstate shows the
// pane; closing it writes an explicit `pane=0` (viewstate) / `preview=false`
// (URL) so the closed choice sticks. `pane` and `panew` are independent:
// turning the pane off keeps a dragged width, so re-opening the folder
// restores it.
//
// Width is a FRACTION of the split container (PANE_DEFAULT_FRAC when nothing
// is saved), rendered as a percentage flex-basis — so the pane keeps its
// proportion when the window resizes, which a resolved pixel width never did.
// Nothing needs measuring for that: a percentage is correct before the first
// paint, whatever the container turns out to be. The pixel floors survive as
// CSS min-widths (.listing-pane-slot / .listing-main) and as the drag's clamp.
import { useRef, useState } from "react";
import { replaceSearch } from "@platform/lib/router";
import { getViewState, setViewState } from "@platform/lib/viewstate";

const PANE_MIN_W = 220;
const LIST_MIN_W = 60;
// Dragging the divider within this many pixels of the container's right edge
// closes the pane on release (the clamp holds the pane at PANE_MIN_W during
// the drag, so the intent is read from the raw cursor position instead).
const PANE_CLOSE_W = 110;
export const PANE_DEFAULT_FRAC = 0.5;

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

// The divider drag, in one pure step: the cursor's distance from the
// container's right edge is the pane's wanted PIXEL width, clamped by the
// shared floors and then divided back out into the fraction that is what
// actually gets stored and rendered. A container with no width (unmeasurable,
// zero-sized) has no meaningful fraction, so the caller keeps what it had.
export function dragPaneFrac(containerW: number, rawPx: number): number | null {
  if (!(containerW > 0)) return null;
  return clampPaneWidth(containerW, rawPx) / containerW;
}

// Parse the `panew` viewstate value. It holds a FRACTION of the split
// container ("0.42"); null = nothing saved, so the caller uses
// PANE_DEFAULT_FRAC and treats the width as unchosen.
//
// Values greater than 1 are LEGACY PIXEL widths from the previous model and
// are ignored as if absent — not translated, because the pixels were measured
// against a container this window may not have (that mismatch is the whole
// reason for the fraction), and the folder simply re-opens at the default
// until the user drags it again.
export function parsePaneFrac(raw: string | null): number | null {
  const f = parseFloat(raw || "");
  if (!Number.isFinite(f) || f <= 0 || f > 1) return null;
  return f;
}

// Shared by resolvePane and any other view (Preview.tsx's topbar-hiding
// check) that needs to know whether the pane is showing for a path without
// wanting its width too. `preview=true`/`preview=false` — the owner's literal
// format (any other value reads as absent, falling back to the saved state).
// No saved state means ON by default; only an explicit `pane=0` (a prior
// close) turns it off.
export function paneIsOpen(fsPath: string): boolean {
  const urlPreview = new URLSearchParams(location.search).get("preview");
  if (urlPreview !== null) return urlPreview === "true";
  return new URLSearchParams(getViewState(fsPath)).get("pane") !== "0";
}

// `frac` is null when this folder has saved no width of its own (or saved a
// legacy pixel one) — the caller opens at PANE_DEFAULT_FRAC and remembers that
// the width was never chosen.
function resolvePane(fsPath: string): { on: boolean; frac: number | null } {
  const s = new URLSearchParams(getViewState(fsPath));
  return { on: paneIsOpen(fsPath), frac: parsePaneFrac(s.get("panew")) };
}

// Merge the pane keys into this folder's saved state without touching a saved
// sort (and vice versa — setSort merges the same way). A null fraction (still
// at the default half) isn't persisted — only a dragged fraction is a choice
// worth remembering. The two keys are INDEPENDENT: `panew` outlives a
// toggle-off, so closing the pane and coming back to the folder re-opens at
// the fraction that was dragged rather than at the default.
//
// Three decimals is the whole of the precision a split is worth: it is a
// tenth of a percent of the container, well under a pixel on any window, and
// it keeps the saved string short and readable.
//
// `pane` only ever stores the OFF choice (`"0"`) — on is the default, so
// nothing needs persisting for it; a stale `pane=1` from before the default
// flipped is just as good as no key at all (resolvePane treats anything but
// `"0"` as on).
function savePaneState(fsPath: string, on: boolean, frac: number | null): void {
  const s = new URLSearchParams(getViewState(fsPath));
  if (on) s.delete("pane");
  else s.set("pane", "0");
  if (frac !== null) s.set("panew", String(Math.round(frac * 1000) / 1000));
  else s.delete("panew");
  const qs = s.toString();
  setViewState(fsPath, qs ? "?" + qs : "");
}

// `enabled=false` (an embedded Listing — the preview pane's own `_listing`
// mode) turns the whole feature off at the source: the pane never resolves
// from URL/viewstate, stays off, and the toggle is inert — no nesting.
export function usePreviewPane(fsPath: string, enabled = true) {
  // Visibility restores URL-first (resolvePane: `?preview=true` wins, then the
  // folder's saved viewstate); the width fraction is viewstate-only. Toggling
  // writes BOTH: the URL (replaceSearch, like setSort — on sets
  // `preview=true`, off deletes it; navigate() then carries the param between
  // folders, making the pane sticky) and the viewstate (so a folder re-opened
  // from a clean URL remembers).
  //
  // `sized` is provenance, not geometry: did the USER choose this fraction
  // (restored from `panew`, or dragged this session), or is it just
  // PANE_DEFAULT_FRAC? Only a chosen fraction is persisted — otherwise a plain
  // toggle would write the default into `panew` as though it had been dragged.
  // It rides in state rather than a ref so every setPane updater can read it.
  const [pane, setPane] = useState<{ on: boolean; frac: number; sized: boolean }>(() => {
    const r = enabled ? resolvePane(fsPath) : { on: false, frac: null };
    return { on: r.on, frac: r.frac ?? PANE_DEFAULT_FRAC, sized: r.frac !== null };
  });
  const splitRef = useRef<HTMLDivElement>(null);

  const togglePane = () => {
    if (!enabled) return;
    setPane((prev) => {
      const next = { ...prev, on: !prev.on };
      const params = new URLSearchParams(location.search);
      if (next.on) params.set("preview", "true");
      else {
        // Explicit `false`, not a deleted param — on is the default now, so
        // an absent param would reopen the pane on the next load/nav.
        params.set("preview", "false");
        // The pane's mode param has no pane to describe once it's closed.
        params.delete("_panelMode");
      }
      const qs = params.toString();
      replaceSearch(location.pathname + (qs ? "?" + qs : ""));
      savePaneState(fsPath, next.on, next.sized ? next.frac : null);
      return next;
    });
  };

  // The divider drag: pointer capture keeps the drag alive when the cursor
  // crosses into the pane's iframe (which would otherwise swallow mousemove).
  const onDividerPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const divider = e.currentTarget;
    divider.setPointerCapture(e.pointerId);
    divider.classList.add("dragging");
    // The pre-drag fraction and provenance, captured once: nothing else can
    // change them while this drag owns the pointer.
    const startFrac = pane.frac;
    const startSized = pane.sized;
    let frac = pane.frac;
    let raw = Infinity;
    let moved = false;
    const onMove = (ev: PointerEvent) => {
      const rect = splitRef.current?.getBoundingClientRect();
      if (!rect) return;
      moved = true;
      // The pane is the right side: its width is the distance from the cursor
      // to the container's right edge, run through the shared FS-12 clamps and
      // divided back into a fraction of the container (dragPaneFrac).
      raw = rect.right - ev.clientX;
      const next = dragPaneFrac(rect.width, raw);
      if (next === null) return;
      frac = next;
      setPane((prev) => (prev.frac === frac ? prev : { ...prev, frac }));
    };
    const onUp = () => {
      divider.classList.remove("dragging");
      divider.removeEventListener("pointermove", onMove);
      divider.removeEventListener("pointerup", onUp);
      divider.removeEventListener("pointercancel", onUp);
      // Released with the cursor (nearly) at the right edge: close the pane,
      // keeping the pre-drag fraction so re-opening restores it.
      if (moved && raw < PANE_CLOSE_W) {
        const params = new URLSearchParams(location.search);
        // Explicit `false` — see togglePane: on is the default now.
        params.set("preview", "false");
        params.delete("_panelMode");
        const qs = params.toString();
        replaceSearch(location.pathname + (qs ? "?" + qs : ""));
        setPane({ on: false, frac: startFrac, sized: startSized });
        savePaneState(fsPath, false, startSized ? startFrac : null);
        return;
      }
      // Only a drag that actually moved the divider is a chosen fraction; a
      // bare click on it leaves the default unpersisted.
      const sized = startSized || moved;
      if (moved) setPane((prev) => (prev.sized ? prev : { ...prev, sized: true }));
      savePaneState(fsPath, true, sized ? frac : null);
    };
    divider.addEventListener("pointermove", onMove);
    divider.addEventListener("pointerup", onUp);
    divider.addEventListener("pointercancel", onUp);
  };

  return { pane, splitRef, togglePane, onDividerPointerDown };
}

// Same URL reflection Listing does for a saved sort: a pane restored CLOSED
// from saved viewstate (URL carried no `preview`) puts `preview=false` on the
// address bar so refresh, bookmarks and onward navigation (which carries the
// param) all see the shown state. Only ever ADDS the param — a URL without it
// and a folder with no saved close (on is the default) keep the clean URL,
// and an explicit `?preview=` value stays authoritative (resolvePane already
// read it).
export function reflectPaneInUrl(fsPath: string): void {
  if (new URLSearchParams(location.search).get("preview") !== null) return; // URL is authoritative
  if (new URLSearchParams(getViewState(fsPath)).get("pane") !== "0") return; // on is the default
  const params = new URLSearchParams(location.search);
  params.set("preview", "false");
  replaceSearch(location.pathname + "?" + params.toString());
}
