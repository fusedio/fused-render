// Sidebar self-update affordance. Renders nothing until /api/config's
// `update` field says a newer version exists (packaged mac app only — the
// field is absent everywhere else), then shows an "Update available" row that
// expands into a small panel. DMG installs get an install button with download
// progress; brew-managed installs get the exact `brew upgrade` command to run
// by hand — the app never runs brew itself.
//
// The poll itself lives in platform/lib/update-status.ts, shared with the
// collapsed rail's dot and the Settings popover's own row — see that file's
// header for why. Once the install lands, installed_version drifts from the
// running version and ServerStatusBanner's restart card takes over — so the
// row drops to a plain "Ready to restart" status line with nothing to expand,
// and the restart card carries the button and the wording.
import { useState } from "react";

import { updateInstall } from "@platform/lib/api";
import {
  pokeUpdateStatus,
  setUpdateStatus,
  updateLabel,
  updateRelevant,
  useUpdateStatus,
} from "@platform/lib/update-status";

function formatProgress(done: number | null, total: number | null): string {
  if (total) return `${Math.min(100, Math.round((100 * (done ?? 0)) / total))}%`;
  if (!done) return "";
  return `${Math.round(done / (1024 * 1024))} MB`;
}

export default function UpdateBadge() {
  const status = useUpdateStatus();
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);

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

  const copyCommand = async () => {
    if (!status.manual_command) return;
    await navigator.clipboard.writeText(status.manual_command);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  const label = updateLabel(status);
  const dot = <span className="update-badge-dot" aria-hidden="true" />;

  // The installed state has no panel of its own — ServerStatusBanner's restart
  // card says the rest — so the row is a status line, not a dead toggle.
  if (status.state === "installed") {
    return (
      <div className="update-badge">
        <div className="update-badge-row update-badge-row-static">
          {dot}
          {label}
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
      </button>
      {open && (
        <div className="update-badge-panel">
          {status.state === "available" && status.method === "brew" && (
            <>
              <div className="update-badge-text">
                Installed with Homebrew — run this in your terminal:
              </div>
              {status.manual_command && (
                <div className="update-badge-command">
                  <code>{status.manual_command}</code>
                  <button type="button" className="update-badge-copy" onClick={copyCommand}>
                    {copied ? "Copied" : "Copy"}
                  </button>
                </div>
              )}
            </>
          )}
          {status.state === "available" && status.method !== "brew" && (
            <>
              <div className="update-badge-text">
                Downloads the new version and installs it in place.
              </div>
              <button type="button" className="update-badge-action" onClick={install}>
                Update to v{status.latest_version}
              </button>
            </>
          )}
          {status.state === "installing" && (
            <div className="update-badge-text">
              Downloading… {formatProgress(status.progress, status.progress_total)}
            </div>
          )}
          {status.state === "error" && (
            <>
              <div className="update-badge-text update-badge-error">
                {status.manual_command
                  ? "Automatic update failed. Run this in your terminal:"
                  : `Update failed: ${status.error ?? "unknown error"}`}
              </div>
              {status.manual_command && (
                <div className="update-badge-command">
                  <code>{status.manual_command}</code>
                  <button type="button" className="update-badge-copy" onClick={copyCommand}>
                    {copied ? "Copied" : "Copy"}
                  </button>
                </div>
              )}
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
