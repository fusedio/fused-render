// Persistent server-health card, rendered at the foot of the shared
// notification stack (NotificationHost owns its placement — this component
// positions nothing). Unlike a toast it has no auto-dismiss: it stays until
// the server answers again, which is why it sits below the transient entries
// rather than shuffling among them.
// Polls /api/config every 5s; what each probe result means (down, reconnected,
// update-refresh, update-restart, auto-reload) lives in lib/server-status.ts —
// this component owns the polling, the timers and the cards. The backend is a
// native app the user launches, so the "down" fix is always "reopen the app",
// not a CLI command. Fully self-contained: mounted once in App's #app root so
// it survives the epoch-keyed view remounts. Styling is .server-status* in
// styles/notifications.css.
//
// TWO OF THE STATES ARE NOT CARDS. Both update cases are the one blocking
// dialog on the shared platform Modal chassis (`UpdateDialog`, D1), because the
// page behind either of them is talking to a version that is going away — every
// click from here is a guess about which side answers:
//   • `update-refresh` — the served bundle moved; a refresh fixes it. Suppressed
//     on a dev server, where the two versions disagree by design, and forced up
//     as a PREVIEW there under `?update_modal=1` — the three modes are
//     `updateDialogMode`'s, stated once in server-status.ts.
//   • the restart case — the disk is ahead of the running app. It USED TO BE a
//     card here with a bare `fused-render://relaunch` link on it, which sat
//     forever and said nothing about whether the press had worked. It is the
//     dialog's "restart" mode now, it POPS ON ITS OWN the moment the install
//     lands (D2 — off the shared update-status store, not a new poll), and it
//     narrates the restart through `restart-flow.ts`'s stages.
//
// WHILE A RESTART IS IN FLIGHT THE "down" CARD IS SUPPRESSED (step 5): the app
// being gone is the restart working, and two surfaces telling opposite stories
// about the same outage is the bug this flow exists to fix. The cap
// (RESTART_GIVE_UP_MS) is what gives the card back.
import { useEffect, useRef, useState } from "react";

import UpdateDialog from "@platform/ui/UpdateDialog";
import { noteRestartProbe, requestRestart, useRestartFlow } from "@platform/lib/restart-store";
import { useUpdateStatus } from "@platform/lib/update-status";
import {
  bannerSurface,
  initialStatus,
  reduceProbe,
  updateDialogMode,
  updateDialogPreview,
  UPDATE_DIALOG_KEY,
  type ProbeResult,
  type ServerBanner,
  type StatusState,
} from "@platform/lib/server-status";

const POLL_MS = 5000;
const PROBE_TIMEOUT_MS = 4000;
const RECONNECT_DISMISS_MS = 5000;

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
  installedVersion: string;
  dev: boolean;
  checkNow: () => void;
} {
  const [state, setState] = useState<StatusState>(initialStatus);
  const [version, setVersion] = useState("");
  const [installedVersion, setInstalledVersion] = useState("");
  const [dev, setDev] = useState(false);
  const probingRef = useRef(false);
  const probeRef = useRef<() => void>(() => {});
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    let disposed = false;
    let dismissTimer: number | undefined;

    async function probe() {
      if (probingRef.current) return;
      probingRef.current = true;
      let result: ProbeResult = { ok: false };
      const ctrl = new AbortController();
      const timeout = window.setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS);
      try {
        const res = await fetch("/api/config", { cache: "no-store", signal: ctrl.signal });
        if (res.ok) {
          const body = await res.json();
          result = {
            ok: true,
            version: typeof body.version === "string" ? body.version : undefined,
            installedVersion:
              typeof body.installed_version === "string" ? body.installed_version : null,
            dev: body.dev === true,
          };
        }
      } catch {
        result = { ok: false };
      } finally {
        window.clearTimeout(timeout);
        probingRef.current = false;
      }
      if (disposed) return;

      // EVERY probe, in flight or not — the restart store needs the version a
      // healthy probe reports (so a press knows what was running) as much as it
      // needs the failures that follow one. Fed before the reload check below
      // on purpose: `reduceProbe` owning the reload is what keeps this flow from
      // having a second one (see restart-flow.ts's header).
      noteRestartProbe({ ok: result.ok, version: result.version ?? null });

      const wasDown = stateRef.current.banner === "down";
      const { state: next, reload } = reduceProbe(stateRef.current, result, BUILD_VERSION);
      if (reload) {
        // Server came back updated — the tab was blocked anyway, and views are
        // URL-synced, so swap in the new shell without asking.
        window.location.reload();
        return;
      }
      if (result.version) setVersion(result.version);
      if (result.installedVersion) setInstalledVersion(result.installedVersion);
      if (result.ok) setDev(result.dev === true);
      setState(next);
      if (next.banner === "reconnected") {
        if (wasDown) {
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

    probeRef.current = probe;
    const interval = window.setInterval(() => {
      if (document.visibilityState !== "hidden") probe();
    }, POLL_MS);

    const onVisible = () => {
      if (document.visibilityState === "visible") probe();
    };
    // "online" probes even while hidden — a WiFi reconnect shouldn't wait for
    // the next visibilitychange to clear the banner.
    const onOnline = () => probe();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onOnline);
    window.addEventListener("focus", onVisible);

    return () => {
      disposed = true;
      window.clearInterval(interval);
      window.clearTimeout(dismissTimer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("focus", onVisible);
    };
  }, []);

  return {
    banner: state.banner,
    version,
    installedVersion,
    dev,
    checkNow: () => probeRef.current(),
  };
}

export default function ServerStatusBanner() {
  const { banner, version, installedVersion, dev, checkNow } = useServerStatus();
  const mode = updateDialogMode(dev, searchOverride(), storedDialogOverride());
  // The SHARED update poll (platform/lib/update-status), the same store the
  // sidebar badge and the Settings row read — no second timer, and it is what
  // makes the restart dialog PROACTIVE (D2): `state === "installed"` is the
  // instant the swap finished, reported on that store's 2 s busy cadence, where
  // the banner's own `update-restart` has to wait for the next 5 s probe to
  // notice the disk moved. Whichever says so first is enough.
  const update = useUpdateStatus();
  const flow = useRestartFlow();
  // WHAT GOES ON SCREEN is a pure decision (server-status.ts `bannerSurface`) —
  // the restart dialog outranking the "down" card is the whole of step 5, and a
  // rule two surfaces have to agree on should be a test, not a reading of the
  // `if`s below.
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
  // paint a blank one. It outranks the down card and the real restart dialog
  // deliberately: the
  // flag is an explicit "show me this dialog", and it is set by hand.
  if (mode === "preview") {
    const preview = updateDialogPreview(searchOverride(), storedDialogOverride());
    if (preview?.kind === "restart") {
      return (
        <UpdateDialog
          kind="restart"
          version={version}
          installedVersion={installedVersion || version}
          stage={preview.stage}
          onRestart={requestRestart}
        />
      );
    }
    return <UpdateDialog kind="refresh" version={version} buildVersion={BUILD_VERSION} />;
  }

  if (surface === "restart-dialog") {
    return (
      <UpdateDialog
        kind="restart"
        version={version}
        // The store's `latest_version` is the fallback for the seconds between
        // the install landing and the next probe re-reading the disk: the
        // dialog's title is a version number and must never read "v".
        installedVersion={installedVersion || update?.latest_version || version}
        stage={flow.stage}
        onRestart={requestRestart}
      />
    );
  }

  if (surface === "none") return null;

  if (surface === "reconnected") {
    return (
      <div className="server-status server-status-reconnected" role="status" aria-live="polite">
        Reconnected — fused-render is back.
      </div>
    );
  }

  if (surface === "refresh-dialog") {
    return <UpdateDialog kind="refresh" version={version} buildVersion={BUILD_VERSION} />;
  }

  return (
    <div className="server-status server-status-down" role="status" aria-live="polite">
      <div className="server-status-title">fused-render isn't running</div>
      <div className="server-status-body">
        The app that powers this page has stopped or was closed. Reopen the fused-render app, and
        this page will reconnect on its own.
      </div>
      {/* fused-render://launch (D128): the OS starts the app, the server-boot
          makes the next probe succeed, and this page reconnects on its own —
          the link opens no tab and navigates nowhere. */}
      <a className="server-status-launch" href="fused-render://launch">
        Start fused-render
      </a>
      <button type="button" className="server-status-retry" onClick={checkNow}>
        Check again
      </button>
    </div>
  );
}
