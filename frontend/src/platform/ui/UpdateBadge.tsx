// Sidebar self-update affordance. Renders nothing until /api/config's
// `update` field says a newer version exists (packaged mac app only — the
// field is absent everywhere else), then shows an "Update available" row that
// expands into a small panel — an accordion: the row wears a chevron, and row
// and panel share one border so the open state reads as a single group.
//
// ONE install path for every install type (D742), so this panel has exactly
// one button and never mentions Homebrew: the in-app install downloads and
// swaps the same version-verified bundle whichever tool put it there, and the
// app never runs brew on itself (the cask's `uninstall quit:` would quit the
// app mid-upgrade — see fused_render/update/mac.py). Once the button is
// pressed the panel only points at the Activity dock — the bytes, the phase
// and the Cancel are on the dock's `sys:update:<version>` row, never here
// (see INSTALLING_TEXT below).
//
// The poll itself lives in platform/lib/update-status.ts, shared with the
// collapsed rail's dot and the Settings popover's own row — see that file's
// header for why. Once the install lands, installed_version drifts from the
// running version and ServerStatusBanner's restart card takes over — so the
// row drops to a plain "Ready to restart" status line with nothing to expand
// (no chevron either), and the restart card carries the wording.
import { useState } from "react";

import { updateInstall } from "@platform/lib/api";
import {
  pokeUpdateStatus,
  setUpdateStatus,
  updateLabel,
  updateRelevant,
  useUpdateStatus,
} from "@platform/lib/update-status";

// The install's progress lives in the Activity dock now — a server-owned
// `sys:update:<version>` job (`fused_render/update/mac.py`'s `JOB_PREFIX`)
// with the bytes, the phase and the Cancel on it. This panel says where to
// look and stops there: a second counter here would be the same download
// counted twice, in two places, by two different pollers — and only one of
// them can offer the ✕.
const INSTALLING_TEXT = "Updating — progress is in Activity";

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

export default function UpdateBadge() {
  const status = useUpdateStatus();
  const [open, setOpen] = useState(false);

  if (!status || !updateRelevant(status)) return null;

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
      >
        {dot}
        {label}
        {CHEVRON}
      </button>
      {open && (
        <div className="update-badge-panel">
          {status.state === "available" && (
            <>
              <div className="update-badge-text">
                Downloads and installs the new version.
              </div>
              <button type="button" className="update-badge-action" onClick={install}>
                Update to v{status.latest_version}
              </button>
            </>
          )}
          {status.state === "installing" && (
            <div className="update-badge-text">{INSTALLING_TEXT}</div>
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
