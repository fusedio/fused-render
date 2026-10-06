// Persistent server-health card, rendered at the foot of the shared
// notification stack (NotificationHost owns its placement — this component
// positions nothing). Unlike a toast it has no auto-dismiss: it stays until
// the server answers again, which is why it sits below the transient entries
// rather than shuffling among them.
// Probes /api/health every 5s (4s abort; `probeHealth` in server-status.ts,
// which also copes with an OLDER server that has no such route) — async and cheap on the server, so a
// busy sync threadpool no longer reads as an outage (SPEC §50). Each failure
// is classified (timeout / refused / http-5xx / http-other / parse) and the
// latency of each healthy probe is kept. The VERSION facts (version,
// installed_version, dev) still come from /api/config, but only at mount,
// every CONFIG_EVERY probes (~60s) and whenever the health boot_id moves —
// update detection needs them, the 5s liveness check does not. When a
// failure streak ends (or the boot id moves under a healthy tab) the reducer
// hands back an outage record and this component POSTs it to
// /api/health/outage; a tab closing mid-outage beacons a partial one. What
// each probe result means (slow, down, reconnected, update-refresh,
// update-restart, auto-reload) lives in lib/server-status.ts — this component
// owns the polling, the timers, the reporting and the cards. The backend is a
// native app the user launches, so the "down" fix is always "reopen the app",
// not a CLI command. Fully self-contained: mounted once in App's #app root so
// it survives the epoch-keyed view remounts. Styling is .server-status* in
// styles/notifications.css.
//
// ONE OF THE STATES IS NOT A CARD. `update-refresh` — the served bundle
// moved, a refresh fixes it — is a blocking dialog on the shared platform
// Modal chassis (`UpdateDialog`), because the page is talking to a version
// that is going away and every click from here is a guess about which side
// answers. Suppressed on a dev server, where the two versions disagree by
// design, and forced up as a PREVIEW there under `?update_modal=1` — the
// three modes are `updateDialogMode`'s, stated once in server-status.ts.
//
// THE RESTART CASE IS NOT DRAWN HERE AT ALL (SPEC-update-notifications.md).
// It used to be a bare-link card, then `UpdateDialog`'s "restart" mode
// (D1) — both gone now, replaced by a status-bar notification
// (`platform/ui/UpdateNotifier.tsx`) that owns the decision AND the in-flight
// narration through `restart-flow.ts`'s stages. What THIS component still
// owns about a restart is narrower: while one is in flight (or the install
// just landed) the "down" card must stay suppressed (step 5) — the app being
// gone is the restart working, and two surfaces telling opposite stories
// about the same outage is the bug that step fixed. The cap
// (RESTART_GIVE_UP_MS) is what gives the card back.
import { useCallback, useEffect, useRef, useState } from "react";

import UpdateDialog from "@platform/ui/UpdateDialog";
import {
  forgetRestartRecord,
  noteRestartProbe,
  restartStageNow,
  useRestartFlow,
} from "@platform/lib/restart-store";
import { useUpdateStatus } from "@platform/lib/update-status";
import { deepLinkScheme, displayName } from "@platform/lib/flavor";
import {
  bannerSurface,
  initialStatus,
  pendingOutage,
  reduceProbe,
  probeOnTick,
  updateDialogMode,
  updateDialogPreview,
  UPDATE_DIALOG_KEY,
  PROBE_TIMEOUT_MS,
  probeHealth,
  versionFactsFrom,
  type OutageRecord,
  type ProbeResult,
  type ServerBanner,
  type StatusState,
  type VersionFacts,
} from "@platform/lib/server-status";

const POLL_MS = 5000;
const RECONNECT_DISMISS_MS = 5000;
/** /api/config is re-read every this many probes (~60s at POLL_MS). */
const CONFIG_EVERY = 12;
const OUTAGE_URL = "/api/health/outage";

/** The version facts from /api/config; null when that read fails (the caller
 *  keeps its last cached copy — liveness is /api/health's call, not this). */
async function fetchVersionFacts(): Promise<VersionFacts | null> {
  const ctrl = new AbortController();
  const timeout = window.setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS);
  try {
    const res = await fetch("/api/config", { cache: "no-store", signal: ctrl.signal });
    if (!res.ok) return null;
    const body = await res.json();
    return versionFactsFrom(body);
  } catch {
    return null;
  } finally {
    window.clearTimeout(timeout);
  }
}

/** POST one outage record to the server's outage log. Fire-and-forget: a
 *  report that fails is not worth a second outage card. `beacon` is the
 *  `pagehide` path, where only sendBeacon reliably leaves the page. */
function reportOutage(outage: OutageRecord, latencies: number[], beacon: boolean): void {
  try {
    const json = JSON.stringify({
      ...outage,
      visible: document.visibilityState === "visible",
      page: window.location?.pathname ?? "",
      latencies_ms: latencies.slice(-20),
    });
    if (beacon && typeof navigator.sendBeacon === "function") {
      navigator.sendBeacon(OUTAGE_URL, new Blob([json], { type: "application/json" }));
      return;
    }
    void fetch(OUTAGE_URL, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: json,
      keepalive: true,
    }).catch(() => {});
  } catch {
    // A bare test DOM (no fetch/navigator) or a throwing accessor: drop it.
  }
}

// Baked by vite `define`; guarded so bun test (no vite) can import this file.
const BUILD_VERSION = typeof __BUILD_VERSION__ === "undefined" ? "" : __BUILD_VERSION__;

/** The localStorage half of the dev preview flag, read defensively: a private
 *  window (or blocked site data) throws on the accessor itself. */
function storedDialogOverride(): string | null {
  try {
    return window.localStorage.getItem(UPDATE_DIALOG_KEY);
  } catch {
    return null;
  }
}

// `window.location` is absent under the test renderer (react-test-renderer
// mounts NotificationHost with a bare `window`), so the query string is read
// the same defensive way the stored flag is.
function searchOverride(): string {
  try {
    return window.location?.search ?? "";
  } catch {
    return "";
  }
}

function useServerStatus(): {
  banner: ServerBanner;
  version: string;
  dev: boolean;
  checkNow: () => void;
} {
  const [state, setState] = useState<StatusState>(initialStatus);
  const [version, setVersion] = useState("");
  const [dev, setDev] = useState(false);
  const probingRef = useRef(false);
  const probeRef = useRef<() => void>(() => {});
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    let disposed = false;
    let dismissTimer: number | undefined;

    // The version facts, cached between /api/config fetches (see the header).
    let cfg: VersionFacts | null = null;
    let probeCount = 0;

    async function probe(forceConfig = false) {
      if (probingRef.current) return;
      probingRef.current = true;
      let result: ProbeResult;
      try {
        result = await probeHealth();
        // Refresh the version facts at mount, every CONFIG_EVERY probes, and
        // whenever the boot id moved — a new process may be a new version,
        // and the reducer must see THAT probe's version or a same-version
        // "reconnected" would paper over an update for a whole minute.
        const bootMoved = result.bootId !== undefined && result.bootId !== stateRef.current.bootId;
        if (result.ok && (forceConfig || cfg === null || probeCount % CONFIG_EVERY === 0 || bootMoved)) {
          const fresh = await fetchVersionFacts();
          // A failed refresh keeps the cache — EXCEPT across a boot move. The
          // cached facts describe the OLD process; merged onto the new one
          // they would make a version-changing restart read as same-version
          // "reconnected" and nothing would retry /api/config for a minute
          // (bugbot, PR #1399). Dropping the cache makes the next probe ask
          // again (`cfg === null`) and this one carry no version at all.
          cfg = fresh ?? (bootMoved ? null : cfg);
        }
        probeCount += 1;
        if (result.ok && cfg) result = { ...result, ...cfg };
      } finally {
        probingRef.current = false;
      }
      if (disposed) return;

      // EVERY probe, in flight or not — the restart store needs the version a
      // healthy probe reports (so a press knows what was running) as much as it
      // needs the failures that follow one. Fed before the reload check below
      // on purpose: `reduceProbe` owning the reload is what keeps this flow from
      // having a second one (see restart-flow.ts's header).
      noteRestartProbe({ ok: result.ok, version: result.version ?? null });

      const prevBanner = stateRef.current.banner;
      const { state: next, reload, outage } = reduceProbe(stateRef.current, result, BUILD_VERSION);
      // Ahead of the render, so a `pagehide` between this probe and the next
      // render reads the streak this probe just extended (or ended).
      stateRef.current = next;
      // Reported BEFORE a reload below: `keepalive` lets the request outlive
      // the document that sent it.
      if (outage) reportOutage(outage, next.latencies, false);
      if (reload) {
        // Server came back updated — the tab was blocked anyway, and views are
        // URL-synced, so swap in the new shell without asking.
        //
        // The durable restart record goes FIRST. This reload is the end of the
        // restart whether or not the stage machine ever reached `back` (a press
        // made before any healthy probe has no version to compare against, so it
        // cannot), and the fresh document that comes up a moment from now reads
        // that record on start — left behind, it would raise the undismissable
        // dialog over a server that is already fine (bugbot, PR #1214).
        forgetRestartRecord();
        window.location.reload();
        return;
      }
      if (result.version) setVersion(result.version);
      if (result.ok) setDev(result.dev === true);
      setState(next);
      if (next.banner === "reconnected") {
        // Armed on the transition INTO reconnected — from down, or from a
        // healthy tab whose server restarted under it (boot id moved).
        if (prevBanner !== "reconnected") {
          window.clearTimeout(dismissTimer);
          dismissTimer = window.setTimeout(() => {
            // Hide the card but KEEP the rest of the state — `served` in
            // particular. Resetting it would make the next version change
            // look like a first observation (refresh card) instead of the
            // transition that auto-reloads.
            if (!disposed) {
              setState((s) => (s.banner === "reconnected" ? { ...s, banner: "hidden" } : s));
            }
          }, RECONNECT_DISMISS_MS);
        }
      } else {
        // Kill any pending reconnected-dismiss on EVERY other state: left
        // armed, it would fire ~5s later and wipe whatever banner is showing
        // by then — with POLL_MS == RECONNECT_DISMISS_MS, an update card that
        // lands right after a reconnect sits squarely in that window.
        window.clearTimeout(dismissTimer);
      }
    }

    // An explicit check (the "Check again" button, the install-landed wake
    // below) re-reads the version facts too, not just liveness.
    probeRef.current = () => probe(true);
    const interval = window.setInterval(() => {
      // Hidden tabs sit the poll out — unless a restart is in flight, when the
      // hidden tab is the one most likely to miss the app coming back
      // (server-status.ts `probeWhileHidden`).
      if (probeOnTick(document.visibilityState, restartStageNow())) probe();
    }, POLL_MS);

    const onVisible = () => {
      if (document.visibilityState === "visible") probe();
    };
    // "online" probes even while hidden — a WiFi reconnect shouldn't wait for
    // the next visibilitychange to clear the banner.
    const onOnline = () => probe();
    // A tab closing (or navigating) MID-OUTAGE never sees the recovery that
    // would have produced the record, so it leaves a partial one behind.
    const onPageHide = () => {
      const pending = pendingOutage(stateRef.current);
      if (pending) reportOutage(pending, stateRef.current.latencies, true);
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onOnline);
    window.addEventListener("focus", onVisible);
    window.addEventListener("pagehide", onPageHide);

    return () => {
      disposed = true;
      window.clearInterval(interval);
      window.clearTimeout(dismissTimer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("focus", onVisible);
      window.removeEventListener("pagehide", onPageHide);
    };
  }, []);

  // STABLE, so a caller can put it in an effect's deps and have that effect
  // run when the thing it watches changes rather than on every render of this
  // component (the installed-transition probe below is exactly that caller).
  const checkNow = useCallback(() => probeRef.current(), []);

  return {
    banner: state.banner,
    version,
    dev,
    checkNow,
  };
}

export default function ServerStatusBanner() {
  const { banner, version, dev, checkNow } = useServerStatus();
  const mode = updateDialogMode(dev, searchOverride(), storedDialogOverride());
  // The SHARED update poll (platform/lib/update-status), the same store the
  // new Preferences "Updates" section and `UpdateNotifier` read — no second
  // timer, and it is what lets the down card's suppression react PROACTIVELY
  // (D2): `state === "installed"` is the instant the swap finished, reported
  // on that store's 2 s busy cadence, where the banner's own `update-restart`
  // has to wait for the next 5 s probe to notice the disk moved. Whichever
  // says so first is enough.
  const update = useUpdateStatus();
  const flow = useRestartFlow();

  // THE INSTALL LANDING WAKES THE PROBE (Akshil, 2026-09-19). Two facts say
  // "there is a new version on disk" and they arrive on two different clocks:
  // the update store's `installed` (2 s while an install runs) and this
  // component's own `/api/config` read of `installed_version` (~60 s since
  // SPEC §50; `checkNow()` forces that read, so this wake still lands at once).
  // What this still buys, now that nothing in this file reads
  // `installed_version` any more (finding #7, code review — the field used
  // to feed a `setInstalledVersion` that had no reader anywhere, on a false
  // claim that `UpdateNotifier` wanted it; it reads `status?.latest_version`
  // from the update store instead, never this probe): forcing the 5 s probe
  // early still feeds `bannerSurface`'s OWN `banner` field faster, so
  // `update-restart` — the disk-ahead-but-still-healthy suppression this
  // component DOES still own — lands sooner than the next scheduled tick.
  //
  // One probe per transition INTO `installed`, not one per render: the ref
  // re-arms only when the state leaves `installed` again (a fresh check, a
  // later install), so a re-render while the dialog sits on screen costs
  // nothing. This only ASKS — it starts no restart, and `restart-store`'s own
  // wake semantics (`noteRestartProbe`, fed by every probe) are untouched.
  const installedProbedRef = useRef(false);
  useEffect(() => {
    if (update?.state !== "installed") {
      installedProbedRef.current = false;
      return;
    }
    if (installedProbedRef.current) return;
    installedProbedRef.current = true;
    checkNow();
  }, [update?.state, checkNow]);
  // WHAT GOES ON SCREEN is a pure decision (server-status.ts `bannerSurface`) —
  // a restart in flight suppressing the "down" card is the whole of step 5,
  // and a rule two surfaces have to agree on should be a test, not a reading
  // of the `if`s below.
  const surface = bannerSurface({
    banner,
    mode,
    updateState: update?.state,
    stage: flow.stage,
  });

  // PREVIEW, ahead of every banner state and of the `hidden` early return: on a
  // dev server there is no version mismatch to wait for, so a preview gated on
  // `update-refresh` would still show nothing — which is the bug this fixes.
  // The numbers are the real ones (see `updateDialogMode`); `version` arrives
  // with the first probe, the same probe that reports `dev`, so this cannot
  // paint a blank one. It outranks the down card deliberately: the flag is an
  // explicit "show me this dialog", and it is set by hand. Only the refresh
  // dialog can be previewed now — `updateDialogPreview` used to also answer
  // for `UpdateDialog`'s deleted "restart" mode.
  if (mode === "preview" && updateDialogPreview(searchOverride(), storedDialogOverride())) {
    return <UpdateDialog kind="refresh" version={version} buildVersion={BUILD_VERSION} />;
  }

  if (surface === "none") return null;

  // A probe timed out but the down threshold is not reached: the server is
  // most likely busy, not gone. A quiet line, no buttons — there is nothing
  // for the reader to do yet, and "isn't running" would be a guess.
  if (surface === "slow") {
    return (
      <div className="server-status server-status-slow" role="status" aria-live="polite">
        {displayName()} is slow to respond…
      </div>
    );
  }

  if (surface === "reconnected") {
    return (
      <div className="server-status server-status-reconnected" role="status" aria-live="polite">
        Reconnected — {displayName()} is back.
      </div>
    );
  }

  if (surface === "refresh-dialog") {
    return <UpdateDialog kind="refresh" version={version} buildVersion={BUILD_VERSION} />;
  }

  return (
    <div className="server-status server-status-down" role="status" aria-live="polite">
      <div className="server-status-title">{displayName()} isn&rsquo;t running</div>
      <div className="server-status-body">
        The app that powers this page has stopped or was closed. Reopen the {displayName()} app, and
        this page will reconnect on its own.
      </div>
      {/* `<scheme>://launch` (D128): the OS starts the app, the server-boot
          makes the next probe succeed, and this page reconnects on its own —
          the link opens no tab and navigates nowhere. */}
      <a className="server-status-launch" href={`${deepLinkScheme()}://launch`}>
        Start {displayName()}
      </a>
      <button type="button" className="server-status-retry" onClick={checkNow}>
        Check again
      </button>
    </div>
  );
}
