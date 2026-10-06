// Column layout (OpenBot live.js "layout" + chat.js glideRows): panel widths and collapsed state are a per-machine
// preference in localStorage; classes and CSS variables on the page root (`.bots-page`, lib/root.ts — FusedBot put
// them on <body>, which here is the shell's) drive the grid in bots.css.
//   root.lcol / .rcol  the user collapsed the sidebar / hid the preview
//   body.lfit / .rfit  the window is too narrow, so it collapsed / hid itself (with hysteresis)
//   body.lanim / .lfast  the rail is mid-slide (rows morph and glide) / closing (quick slide)
//   root.dragging      a gutter drag is in progress
// This module touches the DOM directly (main, .preview, #botlist, #lcol, #add, the root): it is the plumbing the
// React tree sits on, exactly as in OpenBot. The fit is measured on the page's own <main> (ResizeObserver), never
// window.innerWidth: the shell's sidebar takes width the page cannot use.
import { botsRoot } from "./root";

export const LAYOUT_KEY = "browser-bot.layout";
export interface Layout { lw: number; rw: number; lcol: boolean; rcol: boolean }
export const LAYOUT_DEF: Layout = { lw: 280, rw: 400, lcol: false, rcol: false };
// Thread (middle column) floor and preview floor; keep in step with --mid / --rmin in the CSS.
export const MID_MIN = 450, R_MIN = 280;
// Widths below snap collapse the panel; neither handle pushes the thread below MID_MIN, except the preview drag may collapse the sidebar to make room.
export const LIM = { l: { min: 180, max: 560, snap: 130, shut: 72 }, r: { min: R_MIN, max: 900, snap: 150, shut: 0 } } as const;
export const FIT_HYST = 24;
export type Side = "l" | "r";

function loadLayout(): Layout {
  try { return { ...LAYOUT_DEF, ...JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}") }; } catch { return { ...LAYOUT_DEF }; }
}
let layout: Layout = typeof localStorage === "undefined" ? { ...LAYOUT_DEF } : loadLayout();
export const getLayout = (): Layout => layout;

// ------------------------------------------------------------------ pure ----
/** Narrow-window flag with hysteresis: on below 0 px to spare, off from FIT_HYST px, else unchanged. */
export const hyst = (was: boolean, spare: number): boolean => (spare < 0 ? true : spare >= FIT_HYST ? false : was);

/** fitPreview's arithmetic: first the sidebar collapses to icons (lfit), then the preview hides (rfit). */
export function fitFlags(w: number, l: Layout, was: { lfit: boolean; rfit: boolean }): { lfit: boolean; rfit: boolean } {
  const need = 12 + MID_MIN + (l.rcol ? 0 : R_MIN);
  const lfit = !l.lcol && hyst(was.lfit, w - Math.min(l.lw, w * 0.4) - need);
  const sidebar = l.lcol || lfit ? 72 : Math.min(l.lw, w * 0.4);
  return { lfit, rfit: hyst(was.rfit, w - sidebar - 12 - MID_MIN - R_MIN) };
}

/** A gutter drag step: the new layout for a raw width (px) on `side`, given the room left for it. */
export function dragStep(l: Layout, side: Side, raw: number, room: number): Layout {
  const L = LIM[side], key = side === "l" ? "lw" : "rw", col = side === "l" ? "lcol" : "rcol";
  const next = { ...l, [col]: raw < L.snap };
  if (!next[col]) next[key] = Math.max(L.min, Math.min(L.max, raw, room));
  return next;
}

// ------------------------------------------------------------------ DOM ----
const body = botsRoot;
const mainEl = () => body().querySelector(":scope > main") as HTMLElement | null;
const previewEl = () => mainEl()?.querySelector(".preview") as HTMLElement | null;
const listEl = () => body().querySelector("#botlist");
// Header buttons (collapse toggle, +) change slot when the header restacks; FLIP them so they glide instead of hopping.
const railBtns = (): HTMLElement[] => ["#lcol", "#add"].map((id) => body().querySelector<HTMLElement>(id)).filter((x): x is HTMLElement => !!x);

/** Toggle a root class (the side app uses hasapp / sideapp the same way). */
export const bodyClass = (name: string, on: boolean): void => { body().classList.toggle(name, on); };
export const railShut = (): boolean => body().classList.contains("lcol") || body().classList.contains("lfit");
export const previewShown = (): boolean => !body().classList.contains("rcol") && !body().classList.contains("rfit");

/** Each `.bot` row's offsetTop by data-id (the "before" of a FLIP). */
export function rowOffsets(list: Element | null): Record<string, number> {
  const out: Record<string, number> = {};
  list?.querySelectorAll<HTMLElement>(".bot").forEach((el) => { if (el.dataset.id) out[el.dataset.id] = el.offsetTop; });
  return out;
}

// Rows that changed slot start at their old offset and glide to the new one (FLIP); new rows fade in (.enter).
const glideTimers = new WeakMap<HTMLElement, ReturnType<typeof setTimeout>>();
export function glideRows(list: Element | null, before: Record<string, number>): void {
  if (!list) return;
  const moved: HTMLElement[] = [];
  list.querySelectorAll<HTMLElement>(".bot").forEach((el) => {
    const was = before[el.dataset.id || ""];
    if (was === undefined) { el.classList.add("enter"); return; }
    const d = was - el.offsetTop;
    // A row still carrying .glide from an earlier move would transition *to* the start offset and cancel itself out: snap there with transitions off.
    if (d) { el.classList.remove("glide"); el.style.transition = "none"; el.style.transform = `translateY(${d}px)`; moved.push(el); }
  });
  if (!moved.length) return;
  void (list as HTMLElement).offsetHeight;  // flush layout so the offset lands before the transition is armed
  moved.forEach((el) => {
    el.style.transition = ""; el.classList.add("glide"); el.style.transform = "";
    clearTimeout(glideTimers.get(el)); glideTimers.set(el, setTimeout(() => el.classList.remove("glide"), 360));
  });
}

// Web Animations rather than a CSS transition, so the +'s own width/height transition keeps running while it glides.
export function flipEls(els: HTMLElement[], before: DOMRect[]): void {
  els.forEach((el, i) => {
    const b = before[i]; if (!b) return;
    const r = el.getBoundingClientRect(), dx = b.x - r.x, dy = b.y - r.y;
    if (dx || dy) el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 300, easing: "cubic-bezier(.22,1,.36,1)" });
  });
}

// Preview open/close: pin the section to its open width (--rwlock) for the slide so its content rides along instead of reflowing (see .preview in the CSS).
// Closing keeps the lock (the hidden panel needs it to slide back in); opening drops it once the slide is done so the handle can resize again.
let lockT: ReturnType<typeof setTimeout> | undefined;
export function lockPreview(fn: () => void): void {
  const was = previewShown(), pv = previewEl();
  if (was && pv) body().style.setProperty("--rwlock", pv.getBoundingClientRect().width + "px");
  fn();
  const now = previewShown();
  clearTimeout(lockT);
  if (was && now) body().style.removeProperty("--rwlock");  // a plain resize: never hold the width
  else if (now) lockT = setTimeout(() => body().style.removeProperty("--rwlock"), 350);
}

// Rail collapse/expand: rows morph and glide (FLIP, see glideRows) in step with the panel slide. Not during a drag, which must track the pointer.
let glideT: ReturnType<typeof setTimeout> | 0 = 0, glideFinish: (() => void) | null = null;
export function withRailGlide(fn: () => void): void {
  if (glideFinish) glideFinish();  // a toggle mid-slide: settle the previous one first
  const list = listEl(), was = railShut(), before = rowOffsets(list), btns = railBtns().map((el) => el.getBoundingClientRect());
  // Arm lanim before the toggle: fn() reads layout (fitPreview), and a row laid out once in its collapsed form has nothing left to transition.
  // Same for lfast: closing slides 3x faster, and a transition keeps the duration it started with, so the class must be on before the toggle.
  const shutting = !was, cl = body().classList, main = mainEl();
  cl.add("lanim"); cl.toggle("lfast", shutting);
  fn();
  // A drag that snaps the rail shut (or open) animates like the button does; body.dragging.lanim re-enables main's slide for just that stretch.
  if (was === railShut()) { if (!glideT) cl.remove("lanim", "lfast"); return; }
  glideRows(list, before); flipEls(railBtns(), btns);
  // When the panel slide ends, switch to the icon rail (or back); that reflows the header, so glide the rows over it too.
  // transitionend on main's columns is the exact moment; the timer is a fallback for when the transition never fires (e.g. reduced motion).
  const finish = () => {
    if (glideT) clearTimeout(glideT); glideT = 0; glideFinish = null; main?.removeEventListener("transitionend", onEnd);
    const at = rowOffsets(list), b2 = railBtns().map((el) => el.getBoundingClientRect());
    cl.remove("lanim", "lfast");
    glideRows(list, at); flipEls(railBtns(), b2);
  };
  const onEnd = (e: TransitionEvent) => { if (e.target === main && e.propertyName === "grid-template-columns") finish(); };
  main?.addEventListener("transitionend", onEnd);
  glideFinish = finish;
  glideT = setTimeout(finish, shutting ? 240 : 480);
}

// Narrow windows: first the sidebar collapses to icons (lfit), then the preview hides (rfit); each undoes itself with FIT_HYST px to spare so nothing flaps.
export function fitPreview(): void {
  const main = mainEl(); if (!main) return;
  const cl = body().classList, f = fitFlags(main.clientWidth, layout, { lfit: cl.contains("lfit"), rfit: cl.contains("rfit") });
  cl.toggle("lfit", f.lfit);
  cl.toggle("rfit", f.rfit);
}

export function applyLayout(save: boolean): void {
  withRailGlide(() => lockPreview(() => {
    body().style.setProperty("--lw", layout.lw + "px");
    body().style.setProperty("--rw", layout.rw + "px");
    body().classList.toggle("lcol", !!layout.lcol);
    body().classList.toggle("rcol", !!layout.rcol);
    fitPreview();
  }));
  if (save) try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch { /* storage blocked */ }
}

/** Replace part of the layout and apply it. */
export function setLayout(patch: Partial<Layout>, save = true): void { layout = { ...layout, ...patch }; applyLayout(save); }

/** #lcol: collapse or expand the bot list. */
export function toggleLeft(): void {
  const next = { ...layout, lcol: !layout.lcol };
  // Expanding by hand: if the full sidebar + thread + preview cannot fit, hide the preview rather than let lfit snap the sidebar straight back to icons.
  if (!next.lcol && !next.rcol) {
    const w = mainEl()?.clientWidth || 0;
    if (w - Math.min(next.lw, w * 0.4) - 12 - MID_MIN - R_MIN < 0) next.rcol = true;
  }
  layout = next; applyLayout(true);
}
/** #rcol: show or hide the preview. */
export function toggleRight(): void { layout = { ...layout, rcol: !layout.rcol }; applyLayout(true); }
/** #pclose: hide the preview. */
export function closePreview(): void { layout = { ...layout, rcol: true }; applyLayout(true); }
/** Gutter double-click: that side back to its default width, expanded. */
export function resetSide(side: Side): void {
  const key = side === "l" ? "lw" : "rw", col = side === "l" ? "lcol" : "rcol";
  layout = { ...layout, [key]: LAYOUT_DEF[key], [col]: false }; applyLayout(true);
}

/** Gutter pointerdown: track the pointer until release (pointer capture on the gutter itself). */
export function startGutterDrag(side: Side, g: HTMLElement, e: PointerEvent): void {
  const main = mainEl(); if (!main) return;
  const key = side === "l" ? "lw" : "rw", col = side === "l" ? "lcol" : "rcol", L = LIM[side];
  const other = side === "l" ? main.querySelector(".preview") : main.querySelector(".bots");
  // Width the far panel takes; for the sidebar, computed from layout so it can be asked "what if it were collapsed?".
  const otherW = (lcol: boolean) => side === "l" ? (other?.getBoundingClientRect().width || 0) : (lcol ? 72 : Math.min(layout.lw, main.clientWidth * 0.4));
  const roomFor = (lcol = layout.lcol) => main.clientWidth - otherW(lcol) - 12 - MID_MIN;
  e.preventDefault(); g.setPointerCapture(e.pointerId); g.classList.add("drag"); body().classList.add("dragging");
  const x0 = e.clientX, w0 = layout[col] ? L.shut : layout[key];
  let autoShut = false;
  const move = (ev: PointerEvent) => {
    const raw = w0 + (side === "l" ? ev.clientX - x0 : x0 - ev.clientX);
    if (side === "r") {
      if (!layout.lcol && raw > roomFor()) { layout = { ...layout, lcol: true }; autoShut = true; }
      else if (autoShut && raw <= roomFor(false)) { layout = { ...layout, lcol: false }; autoShut = false; }
    }
    layout = dragStep(layout, side, raw, roomFor());
    applyLayout(false);
  };
  const up = () => { g.removeEventListener("pointermove", move); g.classList.remove("drag"); body().classList.remove("dragging"); applyLayout(true); };
  g.addEventListener("pointermove", move);
  g.addEventListener("pointerup", up, { once: true }); g.addEventListener("pointercancel", up, { once: true });
}

/** Boot: apply the saved layout and refit on every resize of <main>. Returns the teardown. */
export function initLayout(): () => void {
  const main = mainEl();
  applyLayout(false);
  if (!main) return () => {};
  const ro = new ResizeObserver(() => withRailGlide(() => lockPreview(fitPreview)));
  ro.observe(main);
  return () => ro.disconnect();
}
