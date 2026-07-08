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
import { useEffect, useState } from "react";
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
