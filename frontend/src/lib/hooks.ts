// Shared re-render signals. The shell has two distinct "URL changed" tiers
// (mirrors the vanilla shell's route()-vs-syncUpdateButton split):
//
//  - nav epoch:  popstate or an explicit navigate()/navigateUrl(). Route is
//    re-derived and the active view remounts (vanilla rebuilt the view DOM on
//    every route() call — a remount is the faithful equivalent).
//  - url version: ANY history write, including replaceState param writes from
//    iframe runtimes and the layout modes' `_layout` sync. Chrome (bookmark
//    buttons, active-bookmark highlight) re-renders; views do NOT remount.
//
// main.tsx wraps history.replaceState/pushState to dispatch "fused:urlchange"
// (the injected runtime writes params through the parent's history object,
// which fires no native event) — that wrapping is load-bearing for the
// layout modes and the update-bookmark flow, not just for these hooks.
import { useEffect, useRef, useState } from "react";
import { NAV_EVENT } from "./router";

function useEventCounter(events: readonly string[]): number {
  const [n, setN] = useState(0);
  useEffect(() => {
    const bump = () => setN((v) => v + 1);
    for (const ev of events) window.addEventListener(ev, bump);
    return () => {
      for (const ev of events) window.removeEventListener(ev, bump);
    };
    // events is a constant array per call site
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return n;
}

export function useNavEpoch(): number {
  return useEventCounter(["popstate", NAV_EVENT]);
}

export function useUrlVersion(): number {
  return useEventCounter(["popstate", NAV_EVENT, "fused:urlchange"]);
}

// Bookmark store change signal. The localStorage store (lib/bookmarks.ts)
// stays a pure data layer; every UI mutation calls notifyBookmarksChanged()
// so all subscribed components (sidebar, breadcrumb star) re-read it.
const BOOKMARKS_EVENT = "fused:bookmarks";

export function notifyBookmarksChanged(): void {
  window.dispatchEvent(new Event(BOOKMARKS_EVENT));
}

export function useBookmarksVersion(): number {
  return useEventCounter([BOOKMARKS_EVENT]);
}

// Recents store change signal — same pattern as the bookmarks event. The
// recents store (lib/recents.ts) dispatches it itself after every cache
// advance (its mutations are triggered by tracking, not by UI clicks alone,
// so the store owns the notify rather than each call site).
const RECENTS_EVENT = "fused:recents";

export function notifyRecentsChanged(): void {
  window.dispatchEvent(new Event(RECENTS_EVENT));
}

export function useRecentsVersion(): number {
  return useEventCounter([RECENTS_EVENT]);
}

// Run `cb` when the tab regains focus or becomes visible again — the app's
// "re-read cheap state on return" freshness posture (deploy dot, deploy
// pref, account status). One shared subscription instead of per-site
// listener boilerplate, and coalesced: a single tab return fires BOTH
// `focus` and `visibilitychange`, which would double every refresh — calls
// landing in the same tick collapse to one. The callback is kept fresh via
// a ref, so passing an inline closure is fine. Does NOT fire on mount —
// callers own their initial read.
export function useRefreshOnReturn(cb: () => void): void {
  const ref = useRef(cb);
  ref.current = cb;
  useEffect(() => {
    let queued = false;
    const refresh = () => {
      if (queued || document.visibilityState !== "visible") return;
      queued = true;
      window.setTimeout(() => {
        queued = false;
        ref.current();
      }, 0);
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);
}

// Tab title reflects whatever's on screen (a file/dir name, or a static
// label like "Panel"), falling back to the bare app name at the root.
// `undefined` means "not this view's title to set" (e.g. App skips it for
// routes StatView owns) so effect ordering can't clobber a sibling's title.
export function useDocumentTitle(label: string | null | undefined): void {
  useEffect(() => {
    if (label === undefined) return;
    document.title = label ? `${label} – Fused Render` : "Fused Render";
  }, [label]);
}
