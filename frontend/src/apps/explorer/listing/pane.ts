// The preview pane (right-hand split): per-folder visibility/width state and
// the usePreviewPane hook that owns the toggle + divider drag.
//
// Visibility follows the sort's model: an explicit `?preview` in the URL wins
// (and rides along on directory navigation — see lib/router navigate, which
// carries it so the pane is sticky between folders), otherwise this folder's
// saved viewstate (keys `pane`/`panew` alongside `sort`/`order`). Width is
// viewstate-only — a pixel width isn't something a shared link should impose.
// A folder with no saved width opens the pane at HALF the split container
// (width null until the measuring effect resolves it; PANE_FALLBACK_W covers
// the pre-paint frame and the unmeasurable edge). Default off — a folder never
// toggled shows the plain listing exactly as before. `pane` and `panew` are
// independent: turning the pane off keeps a dragged width, so re-opening the
// folder restores it.
import { useLayoutEffect, useRef, useState } from "react";
import { replaceSearch } from "@platform/lib/router";
import { getViewState, setViewState } from "@platform/lib/viewstate";

const PANE_MIN_W = 220;
const LIST_MIN_W = 220;
const PANE_MAX_FRAC = 0.65;
export const PANE_DEFAULT_FRAC = 0.5;
const PANE_FALLBACK_W = 420;

// The one place the FS-12 clamps live, so the drag and the measured default
// cannot disagree. Two independent ceilings: the 65 % fraction (the list stays
// the primary surface) and the LIST_MIN_W floor the list needs in pixels — on
// a narrow window 65 % of the container leaves the list well under 220 px, so
// the pixel ceiling is the binding one there. PANE_MIN_W is applied last: in
// the degenerate case (a container too small to satisfy both minimums) the
// pane keeps its floor and the list scrolls, which is what the old template's
// `min-width` did.
function clampPaneWidth(containerW: number, width: number): number {
  return Math.max(PANE_MIN_W, Math.min(containerW * PANE_MAX_FRAC, containerW - LIST_MIN_W, width));
}

function resolvePane(fsPath: string): { on: boolean; width: number | null } {
  const s = new URLSearchParams(getViewState(fsPath));
  const url = new URLSearchParams(location.search);
  // `preview=true` exactly — the owner's literal format (any other value
  // reads as absent, falling back to the saved state).
  const on = url.get("preview") !== null ? url.get("preview") === "true" : s.get("pane") === "1";
  const w = parseInt(s.get("panew") || "", 10);
  return { on, width: Number.isFinite(w) && w >= PANE_MIN_W ? w : null };
}

// Merge the pane keys into this folder's saved state without touching a saved
// sort (and vice versa — setSort merges the same way). A null width (still at
// the measured-default half) isn't persisted — only a dragged width is a
// choice worth remembering. The two keys are INDEPENDENT: `panew` outlives a
// toggle-off, so closing the pane and coming back to the folder re-opens at
// the width that was dragged rather than re-measuring the default.
function savePaneState(fsPath: string, on: boolean, width: number | null): void {
  const s = new URLSearchParams(getViewState(fsPath));
  if (on) s.set("pane", "1");
  else s.delete("pane");
  if (width !== null) s.set("panew", String(Math.round(width)));
  else s.delete("panew");
  const qs = s.toString();
  setViewState(fsPath, qs ? "?" + qs : "");
}

// `enabled=false` (an embedded Listing — the preview pane's own `_listing`
// mode) turns the whole feature off at the source: the pane never resolves
// from URL/viewstate, stays off, and the toggle is inert — no nesting.
export function usePreviewPane(fsPath: string, enabled = true) {
  // Visibility restores URL-first (resolvePane: `?preview=true` wins, then the
  // folder's saved viewstate); width is viewstate-only. Toggling writes BOTH:
  // the URL (replaceSearch, like setSort — on sets `preview=true`, off deletes
  // it; navigate() then carries the param between folders, making the pane
  // sticky) and the viewstate (so a folder re-opened from a clean URL
  // remembers). Width is clamped live during the divider drag; the max
  // fraction is enforced against the split container's current size.
  const [pane, setPane] = useState<{ on: boolean; width: number | null }>(() =>
    enabled ? resolvePane(fsPath) : { on: false, width: null }
  );
  const splitRef = useRef<HTMLDivElement>(null);
  // Is `pane.width` a width the USER chose (restored from `panew`, or dragged
  // this session), as opposed to the measured half-container default? Only a
  // chosen width is persisted — the measuring effect below fills `pane.width`
  // in, which would otherwise make the default indistinguishable from a drag
  // and let a later toggle write it to `panew`.
  const paneSized = useRef(pane.width !== null);
  // No saved width: default to half the split container, measured at first
  // open (layout effect — before paint, so the pane never flashes another
  // width). The clamps still apply; the fallback constant only covers the
  // unmeasurable edge (ref not mounted yet).
  useLayoutEffect(() => {
    if (!pane.on || pane.width !== null) return;
    const w = splitRef.current?.getBoundingClientRect().width;
    const half = w ? clampPaneWidth(w, w * PANE_DEFAULT_FRAC) : PANE_FALLBACK_W;
    setPane((prev) => (prev.width === null ? { ...prev, width: half } : prev));
  }, [pane.on, pane.width]);

  const togglePane = () => {
    if (!enabled) return;
    setPane((prev) => {
      const next = { ...prev, on: !prev.on };
      const params = new URLSearchParams(location.search);
      if (next.on) params.set("preview", "true");
      else {
        params.delete("preview");
        // The pane's mode param has no pane to describe once it's closed.
        params.delete("_panelMode");
      }
      const qs = params.toString();
      replaceSearch(location.pathname + (qs ? "?" + qs : ""));
      savePaneState(fsPath, next.on, paneSized.current ? next.width : null);
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
    let width = pane.width;
    let moved = false;
    const onMove = (ev: PointerEvent) => {
      const rect = splitRef.current?.getBoundingClientRect();
      if (!rect) return;
      moved = true;
      // The pane is the right side: its width is the distance from the cursor
      // to the container's right edge, run through the shared FS-12 clamps.
      width = clampPaneWidth(rect.width, rect.right - ev.clientX);
      setPane((prev) => (prev.width === width ? prev : { ...prev, width }));
    };
    const onUp = () => {
      divider.classList.remove("dragging");
      divider.removeEventListener("pointermove", onMove);
      divider.removeEventListener("pointerup", onUp);
      divider.removeEventListener("pointercancel", onUp);
      // Only a drag that actually moved the divider is a chosen width; a bare
      // click on it leaves the measured default unpersisted.
      if (moved) paneSized.current = true;
      savePaneState(fsPath, true, paneSized.current ? width : null);
    };
    divider.addEventListener("pointermove", onMove);
    divider.addEventListener("pointerup", onUp);
    divider.addEventListener("pointercancel", onUp);
  };

  return { pane, splitRef, togglePane, onDividerPointerDown };
}

// Same URL reflection Listing does for a saved sort: a pane restored from
// saved viewstate (URL carried no `preview`) puts `preview=true` on the
// address bar so refresh, bookmarks and onward navigation (which carries the
// param) all see the shown state. Only ever ADDS the param — a URL without it
// and a folder without saved pane state keep the clean URL, and an explicit
// `?preview=false` stays authoritative (resolvePane already read it as off).
export function reflectPaneInUrl(fsPath: string): void {
  if (new URLSearchParams(location.search).get("preview") !== null) return; // URL is authoritative
  if (!new URLSearchParams(getViewState(fsPath)).get("pane")) return;
  const params = new URLSearchParams(location.search);
  params.set("preview", "true");
  replaceSearch(location.pathname + "?" + params.toString());
}
