// Sidebar self-update affordance. Renders nothing until /api/config's
// `update` field says a newer version exists (packaged mac app only — the
// field is absent everywhere else), then shows ONE flat row: the button that
// downloads and swaps the bundle. No accordion any more (Akshil, 2026-09-19:
// "we need only download button") — the row that used to say "Update
// available" and hide the action behind a chevron IS the action now, and every
// later state is a plain status line in the same frame: "Downloading…",
// "Installing…", "Ready to restart".
//
// ONE install path for every install type (D767), so this row never mentions
// Homebrew: the in-app install downloads and swaps the same version-verified
// bundle whichever tool put it there, and the app never runs brew on itself
// (the cask's `uninstall quit:` would quit the app mid-upgrade — see
// fused_render/update/mac.py). Once pressed, the bytes, the phase and the
// Cancel live on the Activity dock's `sys:update:<version>` row and on the
// bottom-right progress card (platform/ui/UpdateProgressCard) — never here.
//
// THE SERVER INSTALLS THE NEWEST VERSION IT CAN FIND (Akshil, 2026-09-19:
// "before downloading the version we show, check if there is new version
// available and then download the newer version instead"): the press sends
// the version on screen, the server force-rechecks the manifest, and a newer
// release than the one shown is what gets installed — see
// UpdateManager.install. The poll then shows the version that actually went.
//
// NO RESTART BUTTON. The blocking restart dialog (platform/ui/UpdateDialog,
// raised by ServerStatusBanner the moment the shared store says "installed")
// owns the restart; a second button here would be the same action twice.
//
// The poll itself lives in platform/lib/update-status.ts, shared with the
// collapsed rail's dot and the Settings popover's own row — see that file's
// header for why.
import { useCallback, useEffect, useRef, useState } from "react";

import { updateInstall, type UpdateStatus } from "@platform/lib/api";
import {
  CHECK_RESULT_HOLD_MS,
  checkForUpdates,
  checkNowLabel,
  pokeUpdateStatus,
  setUpdateStatus,
  updateLabel,
  updateRelevant,
  useUpdateStatus,
  type ManualCheckPhase,
} from "@platform/lib/update-status";

// The install's progress lives in the Activity dock now — a server-owned
// `sys:update:<version>` job (`fused_render/update/mac.py`'s `JOB_PREFIX`)
// with the bytes, the phase and the Cancel on it. This panel says where to
// look and stops there: a second counter here would be the same download
// counted twice, in two places, by two different pollers — and only one of
// them can offer the ✕.

// THE SLOT'S IDLE FACE (Akshil, 2026-09-10: "give a check for updates button
// -> where we have update available button"). This component used to render
// nothing until an update existed, so the one place in the app that talks about
// updates was invisible exactly when a person wondered whether there was one.
// Now the same frame, in the same place above Settings, reads "Check for
// updates" while there is nothing to report — one slot, two faces, and the
// install row takes it back the moment a version is found.
//
// Only when the updater is THERE (`status !== null`): an unpackaged dev run has
// no `update` in /api/config and nothing to check against, so it still shows
// nothing — unless the server was started with mac.DEV_MANAGER_ENV, which is
// how this row gets tried against 127.0.0.1 at all.
//
// A `<button>`, quiet: no accent dot (the dot means "there is news", and this
// row is the absence of news), muted text, a refresh glyph trailing where the
// install row keeps its download arrow. The glyph turns while a check is in flight — the one
// motion in the sidebar, and it is the row's own progress — and reduced-motion
// holds it still through styles/reduced-motion.css like every other transition.
const REFRESH = (
  <span className="update-badge-refresh" aria-hidden="true">
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
      <path d="M3 3v5h5" />
    </svg>
  </span>
);

// The install row's trailing mark: a download arrow into a tray — the same
// glyph the Settings popover's update row wears (shell/GlobalSidebar.tsx), so
// the two doors to the same action read as one.
const DOWNLOAD = (
  <span className="update-badge-trail" aria-hidden="true">
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3v12" />
      <path d="m7 10 5 5 5-5" />
      <path d="M4 18.5V19a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-.5" />
    </svg>
  </span>
);

// `version`: the running version as /api/config reports it — the same number
// the Settings row's chip below shows, handed down by the sidebar that already
// holds the config, so the badge adds no request of its own. Only the idle
// row reads it ("Up to date · v0.5.22").
export default function UpdateBadge({ version = null }: { version?: string | null } = {}) {
  const status = useUpdateStatus();
  // The idle row's own phase — local, not in the store: it is about THIS press
  // ("Checking…", then the answer for a few seconds), and the store already
  // says the durable thing (idle / available). A found update is not a phase
  // here at all: the store flips to "available" and the install row takes the
  // slot — the Update button is on screen with no second press.
  const [phase, setPhase] = useState<ManualCheckPhase>("rest");
  const holdTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(holdTimer.current), []);
  // WHEN THE SERVER WAS ALREADY LOOKING (bugbot, PR #1097). A non-forced
  // check() that lands while the auto tick's fetch is out returns at once with
  // "checking" — not an answer, a promise of one. The row must not read that
  // as "Up to date": it stays on "Checking…" with this flag raised, and the
  // store's poll (busy cadence while "checking", pollDelay) brings the real
  // answer within a couple of seconds; the effect below settles the row then.
  const awaiting = useRef(false);
  // Stable (setters and refs only), so the effect below can list it honestly.
  const settle = useCallback((result: UpdateStatus) => {
    let next: ManualCheckPhase = result.check_error ? "failed" : "current";
    // A found update: the install row takes the slot and the phase is not
    // read on that branch.
    if (updateRelevant(result)) next = "current";
    setPhase(next);
    clearTimeout(holdTimer.current);
    holdTimer.current = setTimeout(() => setPhase("rest"), CHECK_RESULT_HOLD_MS);
  }, []);
  useEffect(() => {
    if (!awaiting.current || !status || status.state === "checking") return;
    awaiting.current = false;
    settle(status);
  }, [status, settle]);
  if (!status) return null;

  if (!updateRelevant(status)) {
    const check = async () => {
      if (phase === "checking") return;
      clearTimeout(holdTimer.current);
      setPhase("checking");
      try {
        const result = await checkForUpdates();
        if (result.state === "checking") {
          // Not an answer yet — see `awaiting` above.
          awaiting.current = true;
          return;
        }
        // The server answers a failed fetch with "idle" (nothing found) plus the
        // reason; without reading it, an offline laptop would be told it is up
        // to date. `settle` reads it.
        settle(result);
      } catch {
        // 404 (no updater), offline, server down — say so briefly and go back
        // to offering the button; the poll owns the durable story.
        setPhase("failed");
        holdTimer.current = setTimeout(() => setPhase("rest"), CHECK_RESULT_HOLD_MS);
      }
    };
    return (
      // The live region is the FRAME, not the button (cmux-ux-tester,
      // 2026-09-10): a button that is also its own live region has a reader
      // re-announce the whole control on every label change; on the parent, the
      // change is announced as text — "Checking…", then the answer, once.
      <div className="update-badge" aria-live="polite">
        <button
          type="button"
          className={"update-badge-row update-badge-row-check" + (phase === "checking" ? " is-checking" : "")}
          onClick={() => void check()}
          disabled={phase === "checking"}
        >
          {checkNowLabel(phase, version)}
          {REFRESH}
        </button>
      </div>
    );
  }

  const install = async () => {
    try {
      // Send what THIS render actually shows — `status.latest_version`. The
      // server force-rechecks the manifest before it starts and installs the
      // NEWEST version it finds — the one shown, or a newer one published
      // since (UpdateManager.install). It never installs anything older than
      // what was on screen.
      setUpdateStatus(await updateInstall(status.latest_version));
    } catch {
      // Fall through — the re-armed poll picks up the real state.
    }
    pokeUpdateStatus();
  };

  const dot = <span className="update-badge-dot" aria-hidden="true" />;

  // THE ACTION ROW: the one button. Its label names the version it will
  // fetch; the download arrow trails where the idle face keeps its refresh
  // glyph, so the two faces are one line tall alike.
  if (status.state === "available" && !status.check_only) {
    return (
      <div className="update-badge">
        <button type="button" className="update-badge-row update-badge-row-install" onClick={install}>
          {dot}
          Update to v{status.latest_version}
          {DOWNLOAD}
        </button>
      </div>
    );
  }

  // A failed install: the same button, with the reason on it (title) — a
  // press retries, the same call as the first one.
  if (status.state === "error") {
    return (
      <div className="update-badge" aria-live="polite">
        <button
          type="button"
          className="update-badge-row update-badge-row-install"
          onClick={install}
          title={`Update failed: ${status.error ?? "unknown error"}`}
        >
          {dot}
          Update failed · Try again
          {DOWNLOAD}
        </button>
      </div>
    );
  }

  // EVERY OTHER STATE IS A STATUS LINE, nothing to press:
  //   check-only   the dev-run manager (mac.DEV_MANAGER_ENV) can look but not
  //                swap — say so instead of drawing a button the server refuses
  //   installing   one word for which half is running (Akshil, 2026-09-08:
  //                "just words that give status quickly"); the numbers stay on
  //                the Activity row and the bottom-right progress card
  //   installed    "Ready to restart" — the blocking dialog holds the button
  let line: string;
  let title: string | undefined;
  if (status.state === "installing") {
    line = status.phase === "installing" ? "Installing…" : "Downloading…";
  } else if (status.state === "installed") {
    line = updateLabel(status);
  } else {
    line = `v${status.latest_version} available · dev run`;
    title = `v${status.latest_version} is out. This dev run has no bundle to update — install from the packaged app.`;
  }
  return (
    <div className="update-badge" aria-live="polite">
      <div className="update-badge-row update-badge-row-static" title={title}>
        {dot}
        {line}
        {status.state === "installing" && <span className="update-spinner update-badge-trail" aria-hidden="true" />}
      </div>
    </div>
  );
}
