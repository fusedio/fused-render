// Sidebar self-update affordance. Renders nothing until /api/config's
// `update` field says a newer version exists (packaged mac app only — the
// field is absent everywhere else), then shows an "Update available" row that
// expands into a small panel — an accordion: the row wears a chevron, and row
// and panel share one border so the open state reads as a single group.
//
// ONE install path for every install type (D767), so this panel has exactly
// one button and never mentions Homebrew: the in-app install downloads and
// swaps the same version-verified bundle whichever tool put it there, and the
// app never runs brew on itself (the cask's `uninstall quit:` would quit the
// app mid-upgrade — see fused_render/update/mac.py). Once the button is
// pressed the panel only points at the Activity dock — the bytes, the phase
// and the Cancel are on the dock's `sys:update:<version>` row, never here
// (one word: Downloading… / Installing…).
//
// The poll itself lives in platform/lib/update-status.ts, shared with the
// collapsed rail's dot and the Settings popover's own row — see that file's
// header for why. Once the install lands, installed_version drifts from the
// running version and ServerStatusBanner's restart card takes over — so the
// row drops to a plain "Ready to restart" status line with nothing to expand
// (no chevron either), and the restart card carries the wording.
import { useCallback, useEffect, useId, useRef, useState } from "react";

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
// accordion takes it back the moment a version is found.
//
// Only when the updater is THERE (`status !== null`): an unpackaged dev run has
// no `update` in /api/config and nothing to check against, so it still shows
// nothing — unless the server was started with mac.DEV_MANAGER_ENV, which is
// how this row gets tried against 127.0.0.1 at all.
//
// A `<button>`, quiet: no accent dot (the dot means "there is news", and this
// row is the absence of news), muted text, a refresh glyph where the accordion
// keeps its chevron. The glyph turns while a check is in flight — the one
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

// The accordion's disclosure mark: the row is a toggle, and a chevron is what
// says so before it is clicked. One glyph, not two states of markup — CSS
// rotates it 180° off the row's own `aria-expanded`, so the open state has a
// single source of truth and the screen-reader answer and the visual one
// cannot drift apart.
const CHEVRON = (
  <span className="update-badge-chev" aria-hidden="true">
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="m6 9 6 6 6-6" />
    </svg>
  </span>
);

// `version`: the running version as /api/config reports it — the same number
// the Settings row's chip below shows, handed down by the sidebar that already
// holds the config, so the badge adds no request of its own. Only the idle
// row reads it ("Up to date · v0.5.22").
export default function UpdateBadge({ version = null }: { version?: string | null } = {}) {
  const status = useUpdateStatus();
  const [open, setOpen] = useState(false);
  // The row is the disclosure control; the panel is what it discloses, so the
  // pair is wired together by id — `aria-expanded` alone says a thing opened
  // without saying which.
  const panelId = useId();
  // The idle row's own phase — local, not in the store: it is about THIS press
  // ("Checking…", then the answer for a few seconds), and the store already
  // says the durable thing (idle / available). A found update is not a phase
  // here at all: the store flips to "available", the accordion takes the slot,
  // and `open` is set so the Update button is on screen without a second press.
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
    if (updateRelevant(result)) {
      // The accordion takes the slot; open it so the Update button is on
      // screen without a second press. The phase is not read on that branch.
      setOpen(true);
      next = "current";
    }
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
    setOpen(true);
    try {
      setUpdateStatus(await updateInstall());
    } catch {
      // Fall through — the re-armed poll picks up the real state.
    }
    pokeUpdateStatus();
  };

  const label = updateLabel(status);
  const dot = <span className="update-badge-dot" aria-hidden="true" />;

  // The installed state: a status line AND the way out, right here (Akshil,
  // 2026-09-08: "have the action button there as well so we can restart it
  // directly above the settings item"). Nothing to expand — the button is
  // always drawn, so the row carries no chevron — and the same
  // `fused-render://relaunch` link the ServerStatusBanner's restart card uses,
  // so both surfaces restart the same way: the OS hands the link to the
  // running app, which quits through its normal teardown and respawns from the
  // bundle now on disk.
  if (status.state === "installed") {
    return (
      <div className="update-badge">
        <div className="update-badge-row update-badge-row-static">
          {dot}
          {label}
        </div>
        <div className="update-badge-panel">
          <a className="update-badge-action" href="fused-render://relaunch">
            Restart fused-render
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="update-badge">
      <button
        type="button"
        className="update-badge-row"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={panelId}
      >
        {dot}
        {label}
        {CHEVRON}
      </button>
      {open && (
        <div className="update-badge-panel" id={panelId}>
          {status.state === "available" && !status.check_only && (
            <>
              <div className="update-badge-text">
                Downloads and installs the new version.
              </div>
              <button type="button" className="update-badge-action" onClick={install}>
                Update to v{status.latest_version}
              </button>
            </>
          )}
          {status.state === "available" && status.check_only && (
            // The dev-run manager (mac.DEV_MANAGER_ENV) can look but not swap:
            // say so instead of drawing a button whose press the server refuses.
            <div className="update-badge-text">
              v{status.latest_version} is out. This dev run has no bundle to
              update — install from the packaged app.
            </div>
          )}
          {status.state === "installing" && (
            // One word for where the install is (Akshil, 2026-09-08: "just words
            // that give status quickly"); the numbers stay on the Activity row.
            <div className="update-badge-text">
              {status.phase === "installing" ? "Installing…" : "Downloading…"}
            </div>
          )}
          {status.state === "error" && (
            <>
              <div className="update-badge-text update-badge-error">
                Update failed: {status.error ?? "unknown error"}
              </div>
              <button type="button" className="update-badge-action" onClick={install}>
                Try again
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
