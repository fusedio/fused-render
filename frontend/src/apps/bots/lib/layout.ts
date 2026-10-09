// Column layout (OpenBot live.js "layout" + chat.js glideRows): panel widths and collapsed state are a per-machine
// preference in localStorage; classes and CSS variables on the page root (`.bots-page`, lib/root.ts — FusedBot put
// them on <body>, which here is the shell's) drive the grid in bots.css.
//   root.lcol / .rcol  the user collapsed the sidebar / hid the preview
//   body.lfit / .rfit  the window is too narrow, so it collapsed / hid itself (with hysteresis)
//   body.lanim / .lfast  the rail is mid-slide (rows morph and glide) / closing (quick slide)
//   root.dragging      a gutter drag is in progress
//   root.stage         the live view is open in the preview column (Stage: icon sidebar, chat rail --chatw, the page takes the rest)
//   root.sfull         Stage, but the window is too narrow for it: the live view falls back to a fixed overlay (CSS only)
// This module touches the DOM directly (main, .preview, #botlist, #lcol, #add, the root): it is the plumbing the
// React tree sits on, exactly as in OpenBot. The fit is measured on the page's own <main> (ResizeObserver), never
// window.innerWidth: the shell's sidebar takes width the page cannot use.
import { botsRoot } from "./root";

export const LAYOUT_KEY = "browser-bot.layout";
export interface Layout { lw: number; rw: number; cw: number; lcol: boolean; rcol: boolean }
export const LAYOUT_DEF: Layout = { lw: 280, rw: 400, cw: 340, lcol: false, rcol: false };
// Thread (middle column) floor and preview floor; keep in step with --mid / --rmin in the CSS.
export const MID_MIN = 450, R_MIN = 280;
// Stage: the chat rail's floor and the live page's floor; below 72 + 12 + C_MIN + STAGE_MIN the live view goes full-window (sfull).
export const C_MIN = 300, STAGE_MIN = 480;
// Stage: the live page keeps at least half of <main> (keep in step with the 50% in the Stage grid rule in bots.css); dragging the
// rail STAGE_SHUT px past that closes Stage instead (the drag ends there: once Stage is off the same gutter is the preview's).
export const STAGE_HALF = 0.5, STAGE_SHUT = 50;
// Widths below snap collapse the panel; neither handle pushes the thread below MID_MIN, except the preview drag may collapse the sidebar to make room.
// The preview has no fixed ceiling: room (the thread floor) is the only one, and it must agree with the grid in bots.css — a lower cap in either
// place leaves the drag collapsing the sidebar for room the pane never takes.
// `c` is the chat rail in Stage (the right gutter resizes it there); it never collapses, and its only ceiling is the room the live page leaves (stageRoom).
export const LIM = { l: { min: 180, max: 560, snap: 130, shut: 72 }, r: { min: R_MIN, max: Infinity, snap: 150, shut: 0 }, c: { min: C_MIN, max: Infinity, snap: 0, shut: 0 } } as const;
export const FIT_HYST = 24;
export type Side = "l" | "r" | "c";

function loadLayout(): Layout {
  try { return { ...LAYOUT_DEF, ...JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}") }; } catch { return { ...LAYOUT_DEF }; }
}
let layout: Layout = typeof localStorage === "undefined" ? { ...LAYOUT_DEF } : loadLayout();
export const getLayout = (): Layout => layout;

// ------------------------------------------------------------------ pure ----
/** Narrow-window flag with hysteresis: on below 0 px to spare, off from FIT_HYST px, else unchanged. */
export const hyst = (was: boolean, spare: number): boolean => (spare < 0 ? true : spare >= FIT_HYST ? false : was);

/** fitPreview's arithmetic: first the sidebar collapses to icons (lfit), then the preview hides (rfit).
 *  sfull: too narrow for Stage (icon sidebar + chat rail floor + live page floor), so the live view goes full-window. */
export function fitFlags(w: number, l: Layout, was: { lfit: boolean; rfit: boolean; sfull?: boolean }): { lfit: boolean; rfit: boolean; sfull: boolean } {
  const need = 12 + MID_MIN + (l.rcol ? 0 : R_MIN);
  const lfit = !l.lcol && hyst(was.lfit, w - Math.min(l.lw, w * 0.4) - need);
  const sidebar = l.lcol || lfit ? 72 : Math.min(l.lw, w * 0.4);
  return { lfit, rfit: hyst(was.rfit, w - sidebar - 12 - MID_MIN - R_MIN), sfull: hyst(!!was.sfull, w - 72 - 12 - C_MIN - STAGE_MIN) };
}

/** Stage: the widest the chat rail may go for a <main> of `w` px — the live page keeps STAGE_MIN, and at least half of <main>. */
export const stageRoom = (w: number): number => Math.min(w - 72 - 12 - STAGE_MIN, w * (1 - STAGE_HALF) - 72 - 12);

/** A gutter drag step: the new layout for a raw width (px) on `side`, given the room left for it. */
export function dragStep(l: Layout, side: Side, raw: number, room: number): Layout {
  if (side === "c") return { ...l, cw: Math.max(LIM.c.min, Math.min(LIM.c.max, raw, room)) };
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
const staged = (): boolean => body().classList.contains("stage");
export const railShut = (): boolean => body().classList.contains("lcol") || body().classList.contains("lfit") || staged();
export const previewShown = (): boolean => staged() || (!body().classList.contains("rcol") && !body().classList.contains("rfit"));
// In Stage the right gutter sits between the chat rail and the live page: it resizes the rail (`c`), not the preview.
const effSide = (side: Side): Side => (side === "r" && staged() ? "c" : side);

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
  const cl = body().classList, f = fitFlags(main.clientWidth, layout, { lfit: cl.contains("lfit"), rfit: cl.contains("rfit"), sfull: cl.contains("sfull") });
  cl.toggle("lfit", f.lfit);
  cl.toggle("rfit", f.rfit);
  cl.toggle("sfull", f.sfull);
}

/** Stage on/off (the store's `fast`): the sidebar folds to icons with the same row glide as a collapse, the preview column widens into the live view. */
export function setStage(on: boolean): void {
  if (staged() === on) return;
  // No lockPreview: leaving Stage with the preview hidden would pin --rwlock at the stage's full width for the next open.
  withRailGlide(() => bodyClass("stage", on));
}

export function applyLayout(save: boolean): void {
  withRailGlide(() => lockPreview(() => {
    body().style.setProperty("--lw", layout.lw + "px");
    body().style.setProperty("--rw", layout.rw + "px");
    body().style.setProperty("--chatw", layout.cw + "px");
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
  side = effSide(side);
  if (side === "c") { layout = { ...layout, cw: LAYOUT_DEF.cw }; applyLayout(true); return; }
  const key = side === "l" ? "lw" : "rw", col = side === "l" ? "lcol" : "rcol";
  layout = { ...layout, [key]: LAYOUT_DEF[key], [col]: false }; applyLayout(true);
}

/** Gutter pointerdown: track the pointer until release (pointer capture on the gutter itself). */
export function startGutterDrag(side: Side, g: HTMLElement, e: PointerEvent): void {
  const main = mainEl(); if (!main) return;
  side = effSide(side);
  if (side === "c") { startRailDrag(main, g, e); return; }
  const key = side === "l" ? "lw" : "rw", col = side === "l" ? "lcol" : "rcol", L = LIM[side];
  const other = side === "l" ? main.querySelector(".preview") : main.querySelector(".bots");
  // Width the far panel takes; for the sidebar, computed from layout so it can be asked "what if it were collapsed?".
  // A hidden preview still measures its last open width (--rwlock keeps it for the slide back in) although its track is 0: count it as 0.
  const otherW = (lcol: boolean) => side === "l" ? (previewShown() && other ? other.getBoundingClientRect().width : 0) : (lcol ? 72 : Math.min(layout.lw, main.clientWidth * 0.4));
  // The sidebar's track is min(--lw, 40%): cap its room the same way, or a drag saves a width the track never shows.
  const roomFor = (lcol = layout.lcol) => Math.min(main.clientWidth - otherW(lcol) - 12 - MID_MIN, side === "l" ? main.clientWidth * 0.4 : Infinity);
  e.preventDefault(); g.setPointerCapture(e.pointerId); g.classList.add("drag"); body().classList.add("dragging");
  const panel = side === "l" ? main.querySelector(".bots") : main.querySelector(".preview");
  const x0 = e.clientX, w0 = layout[col] ? L.shut : shownWidth(panel, layout[key], L.shut);
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

/** Where a drag starts: the width the grid shows for the panel, not the saved one. The saved width has no ceiling but the
 *  window's room at the time, so after the window shrinks the track is clamped below it; a drag counted from the saved
 *  value would then spend its first stretch inside that gap, moving nothing. A panel shown at its shut width or less (the fit
 *  hid it: lfit / rfit; the sidebar's forced icon rail) starts from the saved width instead, so the gutter reopens it from there as before. */
function shownWidth(panel: Element | null, saved: number, shut = 0): number {
  const w = panel?.getBoundingClientRect().width || 0;
  return w > shut && w < saved ? Math.round(w) : saved;
}

// Dragging the Stage rail past the live page's half-width floor closes Stage. App registers the close (lib/cdp.ts handBack(true):
// gives control back if you drive, then leaves Stage) — this module cannot import it without a cycle.
let stageShut: (() => void) | null = null;
export function onStageShut(fn: (() => void) | null): void { stageShut = fn; }

/** Stage's right gutter: drag right widens the chat rail until the live page is down to half of <main> (stageRoom); STAGE_SHUT px
 *  further closes Stage and ends the drag — the rail is saved at the floor, not the overshoot. */
function startRailDrag(main: HTMLElement, g: HTMLElement, e: PointerEvent): void {
  e.preventDefault(); g.setPointerCapture(e.pointerId); g.classList.add("drag"); body().classList.add("dragging");
  const x0 = e.clientX, w0 = shownWidth(main.querySelector(".chat"), layout.cw);
  const up = () => {
    g.removeEventListener("pointermove", move); g.removeEventListener("pointerup", up); g.removeEventListener("pointercancel", up);
    g.classList.remove("drag"); body().classList.remove("dragging"); applyLayout(true);
  };
  const move = (ev: PointerEvent) => {
    const raw = w0 + ev.clientX - x0, room = stageRoom(main.clientWidth);
    layout = dragStep(layout, "c", raw, room); applyLayout(false);
    // up() first: it persists the clamped rail and its applyLayout settles any slide, so the close's own slide is not cut short.
    if (raw > room + STAGE_SHUT && stageShut) { up(); stageShut(); }
  };
  g.addEventListener("pointermove", move);
  g.addEventListener("pointerup", up); g.addEventListener("pointercancel", up);
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
