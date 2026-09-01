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
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@platform/shadcn/ui/collapsible";

const POLL_IDLE_MS = 60_000;
const POLL_BUSY_MS = 2_000;

function formatProgress(done: number | null, total: number | null): string {
  if (total) return `${Math.min(100, Math.round((100 * (done ?? 0)) / total))}%`;
  if (!done) return "";
  return `${Math.round(done / (1024 * 1024))} MB`;
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
  const dot = <span className="size-2 shrink-0 rounded-full bg-primary" aria-hidden="true" />;

  // One command box for both the brew and the failed-install branches: the
  // command verbatim in monospace, and a Copy beside it.
  const commandBox = status.manual_command ? (
    <div className="flex items-center gap-2 rounded-md bg-muted px-2 py-1">
      <code className="min-w-0 flex-1 truncate font-mono text-xs">{status.manual_command}</code>
      <Button variant="outline" size="xs" onClick={copyCommand}>
        {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  ) : null;

  // The installed state has no panel of its own — ServerStatusBanner's restart
  // card says the rest — so the row is a status line, not a dead toggle.
  if (status.state === "installed") {
    return (
      <div className="mx-2 my-0.5 flex h-7 items-center gap-1.5 px-2.5 text-sm text-muted-foreground">
        {dot}
        {label}
      </div>
    );
  }

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mx-2 my-0.5">
      <CollapsibleTrigger
        render={<Button variant="ghost" size="sm" className="w-full justify-start" />}
      >
        {dot}
        {label}
      </CollapsibleTrigger>
      <CollapsibleContent className="flex flex-col gap-2 px-2.5 py-2 text-xs text-muted-foreground">
        {status.state === "available" && status.method === "brew" && (
          <>
            <div>Installed with Homebrew — run this in your terminal:</div>
            {commandBox}
          </>
        )}
        {status.state === "available" && status.method !== "brew" && (
          <>
            <div>Downloads the new version and installs it in place.</div>
            <div>
              <Button variant="outline" size="xs" onClick={install}>
                Update to v{status.latest_version}
              </Button>
            </div>
          </>
        )}
        {status.state === "installing" && (
          <div>Downloading… {formatProgress(status.progress, status.progress_total)}</div>
        )}
        {status.state === "error" && (
          <>
            <div className="text-destructive">
              {status.manual_command
                ? "Automatic update failed. Run this in your terminal:"
                : `Update failed: ${status.error ?? "unknown error"}`}
            </div>
            {commandBox}
            <div>
              <Button variant="outline" size="xs" onClick={install}>
                Try again
              </Button>
            </div>
          </>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}
