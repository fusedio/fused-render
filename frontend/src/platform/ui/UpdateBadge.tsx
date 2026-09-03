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
import { Button } from "@platform/shadcn/ui/button";
import { StatusDot } from "@platform/ui/flow/StatusIcon";

const POLL_IDLE_MS = 60_000;
const POLL_BUSY_MS = 2_000;

function formatProgress(done: number | null, total: number | null): string {
  if (total) return `${Math.min(100, Math.round((100 * (done ?? 0)) / total))}%`;
  if (!done) return "";
  return `${Math.round(done / (1024 * 1024))} MB`;
}

function Command({ command, copied, onCopy }: { command: string; copied: boolean; onCopy: () => void }) {
  return (
    <div className="flex items-center gap-1.5">
      <code className="min-w-0 shrink overflow-x-auto whitespace-nowrap rounded-sm bg-muted px-1.5 py-1 font-mono text-xs">
        {command}
      </code>
      <Button variant="outline" size="xs" onClick={onCopy}>
        {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
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
  // Blue = upcoming: the update is there to take, nothing is wrong.
  const dot = <StatusDot bucket="blue" />;

  // The installed state has no panel of its own — ServerStatusBanner's restart
  // card says the rest — so the row is a status line, not a dead toggle.
  if (status.state === "installed") {
    return (
      <div className="mx-2 my-0.5 flex items-center gap-2 rounded-md bg-sidebar-accent px-2 py-1.5 text-xs">
        {dot}
        {label}
      </div>
    );
  }

  return (
    <div className="mx-2 my-0.5 text-xs">
      <Button
        variant="ghost"
        size="sm"
        className="w-full justify-start gap-2 bg-sidebar-accent/60 hover:bg-sidebar-accent px-2 text-xs"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {dot}
        {label}
      </Button>
      {open && (
        <div className="flex flex-col gap-2 p-2">
          {status.state === "available" && status.method === "brew" && (
            <>
              <p className="m-0 leading-snug text-muted-foreground">
                Installed with Homebrew — run this in your terminal:
              </p>
              {status.manual_command && (
                <Command command={status.manual_command} copied={copied} onCopy={copyCommand} />
              )}
            </>
          )}
          {status.state === "available" && status.method !== "brew" && (
            <>
              <p className="m-0 leading-snug text-muted-foreground">
                Downloads the new version and installs it in place.
              </p>
              <Button size="xs" className="self-start" onClick={install}>
                Update to v{status.latest_version}
              </Button>
            </>
          )}
          {status.state === "installing" && (
            <p className="m-0 leading-snug text-muted-foreground">
              Downloading… {formatProgress(status.progress, status.progress_total)}
            </p>
          )}
          {status.state === "error" && (
            <>
              <p className="m-0 leading-snug">
                {status.manual_command
                  ? "Automatic update failed. Run this in your terminal:"
                  : `Update failed: ${status.error ?? "unknown error"}`}
              </p>
              {status.manual_command && (
                <Command command={status.manual_command} copied={copied} onCopy={copyCommand} />
              )}
              <Button size="xs" className="self-start" onClick={install}>
                Try again
              </Button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
