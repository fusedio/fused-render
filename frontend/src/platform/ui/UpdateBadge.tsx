// Sidebar self-update affordance. Renders nothing until /api/config's
// `update` field says a newer version exists (packaged mac app only — the
// field is absent everywhere else), then shows an "Update available" row that
// expands into a small panel. DMG installs get an install button with download
// progress; brew-managed installs get the exact `brew upgrade` command to run
// by hand — the app never runs brew itself.
//
// Owns its own slow poll (60s idle, 2s while installing) instead of riding
// ServerStatusBanner's 5s one: the two components live in different trees and
// update state changes rarely. Once the install lands, installed_version
// drifts from the running version and ServerStatusBanner's restart card takes
// over — so the row drops to a plain "Ready to restart" status line with
// nothing to expand, and the restart card carries the button and the wording.
import { useEffect, useRef, useState } from "react";

import { getConfig, updateInstall, type UpdateStatus } from "@platform/lib/api";

const POLL_IDLE_MS = 60_000;
const POLL_BUSY_MS = 2_000;

function formatMb(bytes: number | null): string {
  if (!bytes) return "";
  return `${Math.round(bytes / (1024 * 1024))} MB`;
}

export default function UpdateBadge() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | undefined>(undefined);
  const pollRef = useRef<() => void>(() => {});

  useEffect(() => {
    let disposed = false;

    async function poll() {
      let next: UpdateStatus | null | undefined;
      try {
        const config = await getConfig();
        next = config.update ?? null;
        if (!disposed) setStatus(next);
      } catch {
        // Server down — ServerStatusBanner owns that story; keep last state.
      }
      if (disposed) return;
      const busy = next?.state === "installing";
      timerRef.current = window.setTimeout(poll, busy ? POLL_BUSY_MS : POLL_IDLE_MS);
    }

    pollRef.current = () => {
      window.clearTimeout(timerRef.current);
      poll();
    };
    poll();
    return () => {
      disposed = true;
      window.clearTimeout(timerRef.current);
    };
  }, []);

  if (!status) return null;
  const relevant = ["available", "installing", "installed", "error"].includes(status.state);
  if (!relevant) return null;

  const install = async () => {
    setOpen(true);
    try {
      setStatus(await updateInstall());
    } catch {
      // Fall through — the re-armed poll picks up the real state.
    }
    // Re-arm the poll now so installing-progress shows within POLL_BUSY_MS
    // instead of waiting out the idle interval.
    pollRef.current();
  };

  const copyCommand = async () => {
    if (!status.manual_command) return;
    await navigator.clipboard.writeText(status.manual_command);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  const label =
    status.state === "installing"
      ? "Updating…"
      : status.state === "installed"
        ? "Ready to restart"
        : `Update available${status.latest_version ? ` — v${status.latest_version}` : ""}`;
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
              Downloading… {formatMb(status.progress)}
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
