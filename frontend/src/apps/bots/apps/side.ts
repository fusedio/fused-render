// Side app (OpenBot apps.js "side app"): the right column runs a built app instead of the browser view.
// body.hasapp shows the Browser | <app> strip, body.sideapp swaps the browser view for #saframe (bots.css).
// The frame is SideApp.tsx's; this module drives its src directly, as OpenBot drove #saframe, so "same app at the
// same state" keeps the running app instead of reloading it.
import { useSyncExternalStore } from "react";
import type { AppRef } from "../lib/api";
import { bodyClass, getLayout, setLayout } from "../lib/layout";
import { appEmbedBase, applyAppParams, appStateParams, paramString } from "./apps";

export interface SideAppState { name: string; dir: string; params: string }
let sideApp: SideAppState | null = null;
const subs = new Set<() => void>();
const emit = () => { for (const l of [...subs]) l(); };
export const getSideApp = (): SideAppState | null => sideApp;
export function useSideApp(): SideAppState | null {
  return useSyncExternalStore((l) => { subs.add(l); return () => { subs.delete(l); }; }, getSideApp, getSideApp);
}

let frame: HTMLIFrameElement | null = null, frameKey = "", pending = "";
/** SideApp's #saframe ref. */
export function setSideFrame(el: HTMLIFrameElement | null): void {
  frame = el;
  if (el && pending) { el.src = pending; el.dataset.key = frameKey; pending = ""; }
}
function load(src: string, key: string) {
  frameKey = key;
  if (frame) { frame.src = src; if (key) frame.dataset.key = key; else delete frame.dataset.key; }
  else pending = src;
}

export function showAppBeside(a: AppRef | { name?: string; dir: string; params?: AppRef["params"] }): void {
  const params = paramString(a.params);
  sideApp = { name: a.name || (a.dir || "").split("/").pop() || "", dir: a.dir, params };
  const key = appEmbedBase(sideApp.dir, sideApp.params);  // same app at the same state: keep it running rather than reload
  if (frameKey !== key) { applyAppParams(sideApp.params); load(key, key); }
  bodyClass("hasapp", true); bodyClass("sideapp", true);
  if (getLayout().rcol) setLayout({ rcol: false }, true);  // the column was hidden: slide it open
  emit();
}
export const sideAppBrowser = (): void => bodyClass("sideapp", false);
export const sideAppShow = (): void => { if (sideApp) bodyClass("sideapp", true); };
export function closeSideApp(): void {
  sideApp = null;
  bodyClass("hasapp", false); bodyClass("sideapp", false);
  load("about:blank", "");
  emit();
}
/** Reload keeps the state, like a browser does. */
export function reloadSideApp(): void {
  if (sideApp) load(appEmbedBase(sideApp.dir, appStateParams()), frameKey);
}
