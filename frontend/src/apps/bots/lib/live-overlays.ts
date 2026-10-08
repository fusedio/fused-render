// Stand-ins for what headless Chrome never paints. A <select>'s menu, a datalist's suggestions, the date/time/color
// pickers and the context menu open as widgets outside the page and never reach the screencast (an invisibly open menu
// even swallows the next clicks). The live view draws its own over the stage and writes the result into the page:
//   select   a click / Space / Enter / arrows on a closed single <select> -> a menu of its options
//   list     typing in an <input list=…> -> the matching datalist suggestions under the field
//   picker   a click on a date/time/month/week/datetime-local/color input -> the VIEWER browser's own input of that type,
//            laid over the field, so its native picker and typing both work; the value syncs on change
//   context  a right-click -> Back / Forward / Reload, Open link in new tab / Copy link over a link, Copy / Paste
// The store is external (useOverlay) so LiveView's Overlays component renders it; everything else is plain functions.
import { useSyncExternalStore } from "react";
import { act, cur, getState, showToast } from "../state/store";
import { api } from "./api";
import { $, cdp, evalIn, focusCtl, frameMeta, inCtl, nav, onReset } from "./cdp";
import { frameDims, toStageBox, type Box } from "./live";

export interface SelectOpt { t: string; v: string; s: boolean; d: boolean }
export interface CtxItem { label: string; run: () => void }
export type Overlay =
  | { kind: "select"; opts: SelectOpt[]; left: number; top: number; width: number }
  | { kind: "list"; opts: SelectOpt[]; hi: number; left: number; top: number; width: number }
  | { kind: "picker"; type: string; value: string; left: number; top: number; width: number; height: number }
  | { kind: "context"; items: CtxItem[]; left: number; top: number };

let overlay: Overlay | null = null;
const listeners = new Set<() => void>();
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };
export function setOverlay(o: Overlay | null): void { overlay = o; for (const l of [...listeners]) l(); }
export const getOverlay = (): Overlay | null => overlay;
export const useOverlay = (): Overlay | null => useSyncExternalStore(subscribe, () => overlay, () => overlay);
/** Close whatever is open; `refocus` puts the keyboard back on the page (not when the whole window lost focus). */
export function closeOverlay(refocus = true): void { if (overlay) { setOverlay(null); if (refocus) focusCtl(); } }
onReset.push(() => setOverlay(null));

/** The pointer's position in stage coordinates. */
export function stageXY(e: MouseEvent): { x: number; y: number } {
  const sr = $("stage")?.getBoundingClientRect(); return sr ? { x: e.clientX - sr.left, y: e.clientY - sr.top } : { x: 0, y: 0 };
}
/** A page rect [left, top, width, height] in CSS px -> the same box in stage coordinates (null before the first frame). */
export function toStage(r: Box): { left: number; top: number; width: number; height: number } | null {
  const img = $<HTMLImageElement>("fshot"), stage = $("stage");
  if (!img || !stage) return null;
  return toStageBox(r, img.getBoundingClientRect(), stage.getBoundingClientRect(),
    frameDims(frameMeta(), cur()?.viewport, [img.naturalWidth, img.naturalHeight]), { x: stage.scrollLeft, y: stage.scrollTop });
}

// Page-side probes. `window.__fusedSel` remembers the element the overlay stands for; setPageValue writes through it.
const BOX = "(r => [r.left, r.top, r.width, r.height])(el.getBoundingClientRect())";
const PICKERS = ["date", "time", "datetime-local", "month", "week", "color"];
const HIT_PROBE = (where: string) => `(() => { const el = ${where}; if (!el) return null;
  if (el.tagName === "SELECT" && !el.multiple && !(el.size > 1) && !el.disabled) { window.__fusedSel = el;
    return { kind: "select", opts: [...el.options].map((o) => ({ t: o.text, v: o.value, s: o.selected, d: o.disabled })), r: ${BOX} }; }
  if (el.tagName === "INPUT" && ${JSON.stringify(PICKERS)}.includes(el.type) && !el.disabled && !el.readOnly) { window.__fusedSel = el;
    return { kind: "picker", type: el.type, value: el.value, r: ${BOX} }; }
  return null; })()`;
const LIST_PROBE = `(() => { const el = document.activeElement; if (!el || el.tagName !== "INPUT" || !el.list) return null; const v = el.value.toLowerCase();
  const opts = [...el.list.options].map((o) => ({ t: o.label || o.value, v: o.value, s: false, d: false }))
    .filter((o) => !v || o.v.toLowerCase().includes(v) || o.t.toLowerCase().includes(v)).slice(0, 12);
  if (!opts.length) return null; window.__fusedSel = el; return { opts, r: ${BOX} }; })()`;
const CTX_PROBE = (p: { x: number; y: number }) => `(() => { const el = document.elementFromPoint(${p.x}, ${p.y}); const a = el && el.closest("a[href]"); const act = document.activeElement;
  return { href: a ? a.href : null, sel: String(getSelection() || ""), edit: !!act && (act.isContentEditable || act.tagName === "INPUT" || act.tagName === "TEXTAREA") }; })()`;
/** Selected text in the page: an input/textarea's selection, else the document selection. */
export const SEL_TEXT_PROBE = `(() => { const a = document.activeElement; if (a && (a.tagName === "TEXTAREA" || a.tagName === "INPUT") && a.selectionStart != null && a.selectionEnd > a.selectionStart) return a.value.slice(a.selectionStart, a.selectionEnd); return String(getSelection() || ""); })()`;
// The value goes in through the prototype's setter: on a React-controlled input a plain `el.value = v` is swallowed by
// React's value tracker and the page reverts it.
const setPageValue = (v: string, focus: boolean) => cdp("Runtime.evaluate", { expression: `(() => { const el = window.__fusedSel; if (!el) return;
  Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value").set.call(el, ${JSON.stringify(v)});
  el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true })); ${focus ? "el.focus();" : ""} })()` });

type Hit = { kind: "select"; opts: SelectOpt[]; r: Box } | { kind: "picker"; type: string; value: string; r: Box } | null;
/** Open our select menu or picker for what is at page point p (or the focused select when p is null). True when one opened. */
export async function openOverlayAt(p: { x: number; y: number } | null): Promise<boolean> {
  const hit = await evalIn<Hit>(HIT_PROBE(p ? `document.elementFromPoint(${p.x}, ${p.y})` : "document.activeElement"));
  if (!hit || (!p && hit.kind !== "select") || !inCtl()) return false;
  const box = toStage(hit.r); if (!box) return false;
  if (hit.kind === "select") setOverlay({ kind: "select", opts: hit.opts, left: box.left, top: box.top + box.height, width: box.width });
  else setOverlay({ kind: "picker", type: hit.type, value: hit.value, ...box });
  return true;
}
/** Datalist suggestions for the focused input, a moment after a key that changed its text. */
let suggestTimer = 0;
export function scheduleSuggest(): void {
  window.clearTimeout(suggestTimer);
  suggestTimer = window.setTimeout(async () => {
    const hit = await evalIn<{ opts: SelectOpt[]; r: Box } | null>(LIST_PROBE);
    if (!inCtl()) return;
    if (!hit) { if (overlay?.kind === "list") setOverlay(null); return; }
    const box = toStage(hit.r); if (!box) return;
    setOverlay({ kind: "list", opts: hit.opts, hi: 0, left: box.left, top: box.top + box.height, width: box.width });
  }, 60);
}
export function cancelSuggest(): void { window.clearTimeout(suggestTimer); if (overlay?.kind === "list") setOverlay(null); }

/** A row of the select / datalist menu was picked (null: closed without picking). */
export function pickSelect(v: string | null): void {
  setOverlay(null);
  if (v !== null && inCtl()) setPageValue(v, true);
  focusCtl();
}
/** The picker overlay changed: mirror its value into the page field (the overlay stays until it loses focus). */
export function pickerChange(v: string): void { if (inCtl()) setPageValue(v, false); }
export function runItem(it: CtxItem): void { setOverlay(null); if (inCtl()) it.run(); focusCtl(); }

export function copyText(text: string): void {
  navigator.clipboard?.writeText(text).catch(() => showToast({ text: "Could not write to your clipboard", ts: Date.now() / 1000 }));
}
/** The context menu for a right-click at page point p (pointer at e). */
export async function openContextAt(p: { x: number; y: number }, e: MouseEvent): Promise<void> {
  const info = await evalIn<{ href: string | null; sel: string; edit: boolean }>(CTX_PROBE(p));
  const id = getState().sel;
  if (!id || !inCtl()) return;
  const items: CtxItem[] = [{ label: "Back", run: () => nav("back") }, { label: "Forward", run: () => nav("forward") }, { label: "Reload", run: () => nav("reload") }];
  if (info?.href) {
    const href = info.href;
    items.push({ label: "Open link in new tab", run: () => { void act(() => api.tab(id, { tab: "new", url: href }), true); } },
               { label: "Copy link address", run: () => copyText(href) });
  }
  if (info?.sel) items.push({ label: "Copy", run: () => copyText(info.sel) });
  if (info?.edit) items.push({ label: "Paste", run: () => { navigator.clipboard?.readText().then((t) => { if (t && inCtl()) cdp("Input.insertText", { text: t }); })
    .catch(() => showToast({ text: "Your browser refused to share the clipboard; use ⌘V instead", ts: Date.now() / 1000 })); } });
  const at = stageXY(e);
  setOverlay({ kind: "context", items, left: at.x, top: at.y });
}
