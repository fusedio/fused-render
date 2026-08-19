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
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { NAV_EVENT } from "@platform/lib/router";
import { createCloseDeferrer } from "@platform/lib/exit-animation";
import { getClaudeHealth, getConfig } from "@platform/lib/api";
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

/** The hook's return: the two facts, plus a way to ask again. */
export interface SelfFixReadinessState extends SelfFixReadiness {
  /** Re-read after a start attempt — the user may have just installed Claude. */
  recheck: () => void;
}

// The two preconditions a surface can check before it offers a self-fix session
// (SPEC §43, SF-13f). They come from two places because they are two kinds of
// fact, and each already has an owner:
//
//   readOnly       /api/config — an `os.access` on the install root, cheap
//                  enough to ride the payload every page already reads.
//   claudeMissing  GET /api/claude/health — `found`, from the module that is
//                  the package's ONE resolver of the CLI (#621). Its own
//                  endpoint on purpose: those facts are backed by process
//                  spawns behind a disk cache, and that router says plainly why
//                  they may not be bolted onto the config payload.
//
// Reading `found` from anywhere else is the mistake #621 exists to prevent — it
// was written because four independent lists let one CLI produce a working
// Claude-config tab and an `ai_unavailable` on the same machine in the same
// second. A self-fix button refusing on one answer while the health strip beside
// it reports another would be that bug again, in a new place.
//
// ONLY THE BLOCKING ONE. `found` is false when no session can start at all;
// `outdated` and `signed_out` are real findings the health strip reports
// proactively and lib/trouble classifies after a failure, but neither is a
// reason for THIS button to stop offering a session — an outdated CLI may well
// run one, and a signed-out user is told by the card in seconds.
//
// One fetch per page, not per row: a user with three failed downloads mounts
// three of these, and they are all asking the same question about the same
// directory. The PROMISE is what's cached rather than the answer, so three
// simultaneous mounts share one request instead of racing three.
//
// THE LABEL IS CACHED; THE DECISION NEVER IS. That split is the correction to a
// real trap: `claudeMissing` was cached for the page like `readOnly`, and both
// surfaces then answered the click from the cache without asking the server. But
// this feature EXISTS to tell the user to go and install Claude Code — so the
// one state it caches is the one state it is actively asking the user to change,
// and a user who did what the button said, then clicked it again in the same
// tab, was told the binary was still missing until they reloaded. That is worse
// than not pre-checking at all: before the pre-check, the second click started a
// session. So the cached value only ever WORDS the button; every click asks the
// server, which is the only thing that knows the current answer. `recheck` then
// re-reads so the wording catches up on the same interaction rather than at the
// next page load.
//
// `readOnly` really is stable — file permissions on the install root are a
// property of how the app was installed, and a copy that changes owner
// mid-session has bigger problems than a stale label — but it is read through
// the same path, because one request answering both is the point.
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
    // Both, or neither. `allSettled` rather than `all` so one endpoint being
    // unhappy does not throw away the other's answer: a health probe that
    // cannot report is not a finding (the strip makes the same call), and it
    // must not cost the read-only wording that has nothing to do with it.
    const probe: Promise<SelfFixReadiness> = Promise.allSettled([
      getConfig(),
      getClaudeHealth(),
    ]).then(([config, health]) => {
      const ready = {
        readOnly:
          config.status === "fulfilled" && config.value.read_only === true,
        claudeMissing:
          health.status === "fulfilled" && health.value.found === false,
      };
      // A failed read is not remembered as an answer — the next mount asks
      // again rather than inheriting a shrug for the rest of the session.
      //
      // Cleared only if the cache STILL HOLDS THIS PROBE. A probe that has
      // already been replaced — by `recheck`, or by the test seam — is no
      // longer the cache's answer, and its failure is no longer the cache's
      // business: a slow first read that fails after a good recheck would
      // otherwise throw away the fresh answer it knows nothing about, and the
      // next mount would re-ask and race the listeners all over again. The
      // generation guards above stop a stale answer being PAINTED; this stops
      // a stale failure DELETING a current one. Identity rather than the
      // generation counter because that is the exact claim being made — not
      // "nothing has happened since", but "this entry is mine to remove".
      if (
        (config.status === "rejected" || health.status === "rejected") &&
        readinessProbe === probe
      ) {
        readinessProbe = null;
      }
      return ready;
    });
    readinessProbe = probe;
  }
  return readinessProbe;
}

// Every mounted hook, so a re-read reaches all of them. The cache is SHARED —
// three failed rows ask once — so its invalidation has to be shared too, or the
// row whose button was clicked would update its verb while the rows beside it
// went on saying the opposite about the same machine.
const readinessListeners = new Set<(value: SelfFixReadiness) => void>();

// GENERATION, the same device `platform/lib/selffix`'s PollGen uses and for the
// same reason: an answer is only worth applying if it belongs to the question
// still being asked. Each mount also holds a `.then` on the probe that was
// current when it mounted, and `recheck` starts a NEW one — so without this the
// slower first reply lands last and overwrites the recheck, putting the button
// back on "Set up Claude Code" after the server has already accepted a session.
// The exact trap this hook was rewritten to close, re-entering through the
// rewrite.
let readinessGen = 0;

/** Resolve the current probe and hand the value to `apply` — unless a `recheck`
    has since superseded it. */
function deliverReadiness(apply: (value: SelfFixReadiness) => void): void {
  const gen = readinessGen;
  probeReadiness().then((value) => {
    if (gen === readinessGen) apply(value);
  });
}

function refreshReadiness(): void {
  readinessProbe = null;
  readinessGen += 1;
  const gen = readinessGen;
  probeReadiness().then((value) => {
    if (gen !== readinessGen) return;  // a later recheck already superseded this
    for (const notify of [...readinessListeners]) notify(value);
  });
}

/** Test seam: forget the cached probe so a case can serve a different config. */
export function resetSelfFixReadiness(): void {
  readinessProbe = null;
  readinessGen += 1;
}

export function useSelfFixReadiness(): SelfFixReadinessState {
  const [ready, setReady] = useState(NOT_READY);
  useEffect(() => {
    let cancelled = false;
    const apply = (value: SelfFixReadiness) => {
      if (!cancelled) setReady(value);
    };
    readinessListeners.add(apply);
    deliverReadiness(apply);
    return () => {
      cancelled = true;
      readinessListeners.delete(apply);
    };
  }, []);
  // Called after a start attempt resolves: the attempt is the moment the user
  // may have just changed the answer — installed Claude Code, because this
  // button told them to. One re-read, delivered to every mounted row.
  const recheck = useCallback(refreshReadiness, []);
  return { ...ready, recheck };
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
