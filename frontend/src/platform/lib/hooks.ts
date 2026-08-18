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
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { NAV_EVENT } from "@platform/lib/router";
import { createCloseDeferrer } from "@platform/lib/exit-animation";
import { getConfig } from "@platform/lib/api";
import { navReach, subscribeNavReach, type NavReach } from "@platform/lib/nav-history";
import {
  getSidebarState,
  subscribeSidebarState,
  type SidebarState,
} from "@platform/lib/sidebarstate";

export function useEventCounter(events: readonly string[]): number {
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

// Armed-bookmark change signal — same store-owned pattern as recents below:
// armBookmark()/disarmBookmark() (lib/bookmarks.ts) dispatch it themselves,
// because not every disarm site coincides with a url or bookmark-store event
// (the Breadcrumb's pathname-change disarm runs in an effect AFTER the sidebar
// has already rendered against the stale armed value).
const ARMED_EVENT = "fused:armchange";

export function notifyArmedChanged(): void {
  window.dispatchEvent(new Event(ARMED_EVENT));
}

export function useArmedVersion(): number {
  return useEventCounter([ARMED_EVENT]);
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

// Exit animation for an overlay whose CALLER owns the unmount (every dialog is
// `{open && <Modal …/>}`, so the overlay can't hold itself on screen — see
// lib/exit-animation). Returns `closing` — true while the exit runs, i.e. the
// frame budget the `.closing` CSS has to play in — and `requestClose`, which
// every close path (Esc, backdrop, ✕) calls INSTEAD of onClose.
//
// The deferrer is created once and reads `onClose` through a ref, so an inline
// arrow closure as onClose (what every call site passes) doesn't tear down and
// rebuild a pending exit mid-animation.
export function useDeferredClose(
  onClose: () => void,
  durationMs: number,
): { closing: boolean; requestClose: () => void } {
  const [closing, setClosing] = useState(false);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const deferrer = useMemo(
    () => createCloseDeferrer(durationMs, () => closeRef.current(), setClosing),
    [durationMs],
  );
  // Drop a pending close on unmount: the caller may have unmounted the overlay
  // for its own reasons (a navigation) and the timer must not fire into it.
  useEffect(() => () => deferrer.cancel(), [deferrer]);
  return { closing, requestClose: deferrer.request };
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

// Whether the builtin learn mount is attached and browsable. Seeded from the
// boot-time config snapshot, then re-verified by a bounded /api/config poll —
// the one-shot fetch (main.tsx) lands well before the server's background
// automount thread finishes attaching the mount, so the snapshot essentially
// always says false; and the inverse race exists too (rcd survives server
// restarts, so boot can catch the PRIOR run's still-live mount reporting true
// moments before ensure_learn_mount's forced detach rips it out), so the poll
// always runs and follows whatever the fresh answer says. The bound (2s x 60
// = 120s) comfortably exceeds attach_mount's ~70s worst case (ensure_rcd
// spawn + full 60s mount rc timeout, shell/mounts.py) so a slow-but-
// successful mount isn't missed; the cap keeps a dev checkout with no
// bundled learn.zip (never becomes ready) from polling forever. Once any
// mount confirms true, that result is cached at module scope (below) so a
// later remount of the hook doesn't re-litigate it against a stale seed.
// Module-level cache of the last CONFIRMED-true readiness, shared by every
// mount of the hook. Home unmounts/remounts on every visit to "/" (it's a
// route, not persistent chrome like Sidebar), so without this a return visit
// re-seeds from the stale boot `initial` (still false) and restarts the
// bounded poll from scratch — the Learn card would vanish for up to 2s and
// reflow the grid on every trip back to Home, even though readiness was
// already confirmed earlier in the session.
// Per-builtin, and keyed rather than a single flag: a mount confirms only
// itself, and one flag shared between two would mark the other ready the
// moment either attached.
const cachedReady: Record<BuiltinMountKey, boolean> = {
  learn_mount_ready: false,
};

// ONE key now. The Claude Config app stopped being a mount when it became
// native React over its own server bridge (a one-shot
// GET /api/claude-config/status — availability is a property of the
// installation and cannot flip mid-session, which is the only thing the poll
// below exists for), and the Sessions inbox page was deleted outright on
// 2026-08-18 (Tasks supersedes it). The generic is kept rather than inlined:
// the next bundled mount is a key, not a rewrite.
type BuiltinMountKey = "learn_mount_ready";

export function useLearnMountReady(initial: boolean): boolean {
  return useBuiltinMountReady(initial, "learn_mount_ready");
}

function useBuiltinMountReady(initial: boolean, key: BuiltinMountKey): boolean {
  const [ready, setReady] = useState(cachedReady[key] || initial);
  useEffect(() => {
    if (cachedReady[key]) return; // already confirmed — nothing left to poll for
    let cancelled = false;
    let attempts = 0;
    // setInterval fires a new getConfig() every tick without waiting for the
    // previous one to settle, so responses can arrive out of order. Only the
    // newest ISSUED request's response is applied — a straggler from an
    // earlier tick is discarded as stale rather than overwriting a `true` a
    // later request already reported (which would stick permanently, since
    // that `true` had already cleared the interval).
    let latestRequestId = 0;
    const MAX_ATTEMPTS = 60;
    const POLL_MS = 2000;
    const timer = window.setInterval(() => {
      attempts += 1;
      const requestId = ++latestRequestId;
      getConfig().then(
        (fresh) => {
          if (cancelled || requestId !== latestRequestId) return;
          setReady(fresh[key]);
          if (fresh[key]) {
            cachedReady[key] = true;
            window.clearInterval(timer);
          } else if (attempts >= MAX_ATTEMPTS) {
            window.clearInterval(timer);
          }
        },
        () => {
          if (cancelled || requestId !== latestRequestId) return;
          // Transient fetch failure — just try again next tick.
          if (attempts >= MAX_ATTEMPTS) window.clearInterval(timer);
        }
      );
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    // Deliberately empty deps: run once on mount only. Depending on `ready`
    // here would restart the whole bounded poll window from zero every time
    // it changes, and `initial` is only a seed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return ready;
}

/** What a surface needs to know BEFORE it offers a self-fix session. */
export interface SelfFixReadiness {
  /** The installation cannot be written to: a session can diagnose, not fix. */
  readOnly: boolean;
  /** Claude Code is not installed: no session can start at all. */
  claudeMissing: boolean;
}

// The two preconditions a surface can check before it offers a self-fix session
// (SPEC §43, SF-13f), read together off /api/config because they are read
// together and neither is worth its own request. Preferences learns the
// read-only half from the GET /api/selffix snapshot it opens with; the download
// manager's failed rows learn both from here, because they have to word a
// button BEFORE anyone clicks it, and the snapshot costs a directory walk and a
// brew probe.
//
// ONLY WHAT IS ACTUALLY KNOWABLE. `readOnly` is an `os.access`, `claudeMissing`
// is the same binary resolution the spawn uses. Whether Claude is SIGNED IN, or
// over its usage limit, is deliberately absent: neither can be learned without
// running the CLI, and a check that spawned a process per failed row to answer a
// question that changes minute to minute would be worse than the click it saved.
// Those two stay post-hoc, classified from the failure (lib/trouble.ts, §42).
//
// One fetch per page, not per row: a user with three failed downloads mounts
// three of these, and they are all asking the same question about the same
// directory. The PROMISE is what's cached rather than the answer, so three
// simultaneous mounts share one request instead of racing three.
//
// Cached for the whole session, and that is sound in a way the learn-mount poll
// above is not: file permissions on the install root are a property of how the
// app was installed, and a copy that changes owner mid-session has bigger
// problems than a stale button label.
//
// Absence answers FALSE on both — the label falls back to promising an ordinary
// fix. Deliberate: nearly every installation is one the user owns with Claude
// installed, and the failure mode on the rare other one is a button that
// overpromises for a moment, against a server response that still tells the
// truth (`diagnostic`, or the spawn's own "isn't installed") and a session that
// is told in its own prompt. Defaulting the other way would mis-word the button
// for everyone whose config fetch was merely slow.
const NOT_READY: SelfFixReadiness = { readOnly: false, claudeMissing: false };

let readinessProbe: Promise<SelfFixReadiness> | null = null;

function probeReadiness(): Promise<SelfFixReadiness> {
  if (!readinessProbe) {
    readinessProbe = getConfig().then(
      (config) => ({
        readOnly: config.read_only === true,
        claudeMissing: config.claude_missing === true,
      }),
      () => {
        // Let the next mount try again — a transient fetch failure should not
        // pin "ready" for the rest of the session.
        readinessProbe = null;
        return NOT_READY;
      }
    );
  }
  return readinessProbe;
}

/** Test seam: forget the cached probe so a case can serve a different config. */
export function resetSelfFixReadiness(): void {
  readinessProbe = null;
}

export function useSelfFixReadiness(): SelfFixReadiness {
  const [ready, setReady] = useState(NOT_READY);
  useEffect(() => {
    let cancelled = false;
    probeReadiness().then((value) => {
      if (!cancelled) setReady(value);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return ready;
}

// Live sidebar chrome state (platform/lib/sidebarstate) — collapsed flag and
// dragged width, shared so every owner of the frame agrees on the layout.
export function useSidebarState(): SidebarState {
  return useSyncExternalStore(subscribeSidebarState, getSidebarState, getSidebarState);
}

// Whether Back / Forward have anywhere to go (platform/lib/nav-history). Not
// `useNavEpoch` + a read: the answer also changes on `currententrychange`, an
// event that fires on `window.navigation` rather than on `window`, so the
// counter hook above cannot see it.
export function useNavReach(): NavReach {
  return useSyncExternalStore(subscribeNavReach, navReach, navReach);
}
