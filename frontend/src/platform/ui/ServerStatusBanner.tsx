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
// ONE OF THE STATES IS NOT A CARD: `update-refresh` is a blocking dialog on the
// shared platform Modal chassis, because the page behind it is talking to a
// server that no longer serves this bundle — every click from here is a guess
// about which side answers. It is suppressed on a dev server, where the two
// versions disagree by design, and forced up as a PREVIEW there under
// `?update_modal=1` — the three modes are `updateDialogMode`'s, stated once in
// server-status.ts.
import { useEffect, useRef, useState } from "react";

import { Modal } from "@platform/ui/modal/Modal";
import {
  initialStatus,
  reduceProbe,
  updateDialogMode,
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

/** The `update-refresh` case: a BLOCKING dialog, not a card in the stack. The
 *  bundle in this tab is not the one the server serves any more, so there is
 *  nothing on the page behind it worth keeping usable — and one button, because
 *  there is exactly one way out. No ✕, no Esc, no backdrop click: a dismissable
 *  version prompt is a card, and this deliberately is not one.
 *
 *  ON THE SHARED CHASSIS (Akshil, 2026-09-14: "check the delete task modal,
 *  reuse that same component"). It used to be a hand-rolled backdrop + box with
 *  a `.server-update-*` skin of its own, which is how a second dialog vocabulary
 *  gets into the app; it is now `Modal` used exactly the way `EraseTaskModal`
 *  uses it — title, a sentence in the body, the one button in the footer — so it
 *  wears the Delete-task dialog's chrome, tokens and animation for free.
 *
 *  NOT DISMISSABLE, expressed in the chassis' own vocabulary:
 *   • `busy` is the chassis' "this cannot be closed from the chrome" lever. It
 *     drops the ✕ entirely (Modal does not render it while busy) and makes
 *     `decideClose` answer "block" for both Esc and a backdrop press. No
 *     `dismissable` prop exists — this IS that prop under another name, and the
 *     usual reading ("an action is running") is true enough here: the page is
 *     mid-swap onto a version it does not have.
 *   • `onClose` is required by `ModalProps` and is therefore a NO-OP: with
 *     `busy` set, nothing in the chassis can reach it, and a real handler would
 *     only describe a close that must never happen.
 *   • Focus lands on the Refresh button without an `initialFocus` ref, because
 *     the chassis' fallback picks the first focusable outside `.modal-head` and
 *     — with the ✕ gone — that button is the only focusable in the dialog. The
 *     same fact makes the chassis' Tab trap a no-op cycle: first === last, so
 *     Tab and Shift+Tab keep focus where it is.
 *   • `aria-modal` + `aria-labelledby` come from the chassis.
 */
function UpdateDialog({ version }: { version: string }) {
  // ESCAPE IS SWALLOWED FOR THE WHOLE PAGE, which `busy` alone does not do:
  // `busy` only stops the chassis from closing THIS dialog. The stale page
  // behind the scrim is still mounted and still listening — another modal's Esc
  // stack, the sidebar, a peek — so a press here would close something the
  // reader cannot see instead of doing nothing at all. Capture phase on
  // `document` with `stopImmediatePropagation`, because the listeners being
  // headed off are document-level ones that React's synthetic propagation never
  // reaches; the dialog blocks every other input by covering the page, and this
  // is the one key that gets past a scrim.
  useEffect(() => {
    const swallowEscape = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopImmediatePropagation();
    };
    document.addEventListener("keydown", swallowEscape, true);
    return () => document.removeEventListener("keydown", swallowEscape, true);
  }, []);

  return (
    <Modal
      title={`fused-render updated to v${version}`}
      busy
      onClose={() => {}}
      footer={
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => window.location.reload()}
        >
          Refresh page
        </button>
      }
    >
      <p>This page is still on v{BUILD_VERSION}. Refresh to load the new version.</p>
    </Modal>
  );
}

export default function ServerStatusBanner() {
  const { banner, version, installedVersion, dev, checkNow } = useServerStatus();
  const mode = updateDialogMode(dev, searchOverride(), storedDialogOverride());

  // PREVIEW, ahead of every banner state and of the `hidden` early return: on a
  // dev server there is no version mismatch to wait for, so a preview gated on
  // `update-refresh` would still show nothing — which is the bug this fixes.
  // The numbers are the real ones (see `updateDialogMode`); `version` arrives
  // with the first probe, the same probe that reports `dev`, so this cannot
  // paint a blank one. It outranks the down/restart cards deliberately: the
  // flag is an explicit "show me this dialog", and it is set by hand.
  if (mode === "preview") return <UpdateDialog version={version} />;
  if (banner === "hidden") return null;

  if (banner === "reconnected") {
    return (
      <div className="server-status server-status-reconnected" role="status" aria-live="polite">
        Reconnected — fused-render is back.
      </div>
    );
  }

  if (banner === "update-refresh") {
    // Nothing at all on a dev server — see `updateDialogMode` for why the
    // prompt is wrong there rather than merely noisy. ("preview" is already
    // handled above, so only "real" reaches the dialog from here.)
    if (mode === "off") return null;
    return <UpdateDialog version={version} />;
  }

  if (banner === "update-restart") {
    return (
      <div className="server-status server-status-update" role="status" aria-live="polite">
        <div className="server-status-title">
          fused-render v{installedVersion} is installed
        </div>
        <div className="server-status-body">
          The app is still running v{version}. Restart fused-render to finish the update.
        </div>
        {/* fused-render://relaunch: the OS hands the link to the running app,
            which quits through the normal teardown and respawns from the
            bundle on disk. The down-card shows while it's gone, and the
            reconnect probe auto-reloads this page onto the new version. */}
        <a className="server-status-launch" href="fused-render://relaunch">
          Restart fused-render
        </a>
      </div>
    );
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
