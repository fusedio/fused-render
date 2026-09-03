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
// it survives the epoch-keyed view remounts.
import { useEffect, useRef, useState, type ReactNode } from "react";

import {
  initialStatus,
  reduceProbe,
  type ProbeResult,
  type ServerBanner,
  type StatusState,
} from "@platform/lib/server-status";
import { cn } from "@platform/lib/utils";
import { Alert, AlertDescription, AlertTitle } from "@platform/shadcn/ui/alert";
import { Button, buttonVariants } from "@platform/shadcn/ui/button";
import { StatusDot } from "@platform/ui/flow/StatusIcon";
import { bucketText, type StatusBucket } from "@platform/ui/status-colors";

const POLL_MS = 5000;
const PROBE_TIMEOUT_MS = 4000;
const RECONNECT_DISMISS_MS = 5000;

// Baked by vite `define`; guarded so bun test (no vite) can import this file.
const BUILD_VERSION = typeof __BUILD_VERSION__ === "undefined" ? "" : __BUILD_VERSION__;

function useServerStatus(): {
  banner: ServerBanner;
  version: string;
  installedVersion: string;
  checkNow: () => void;
} {
  const [state, setState] = useState<StatusState>(initialStatus);
  const [version, setVersion] = useState("");
  const [installedVersion, setInstalledVersion] = useState("");
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
    checkNow: () => probeRef.current(),
  };
}

// One card shape for every state; the status dot + title tint say which
// (status-colors: red = down, green = back, orange = an update waiting on the
// user). `role="status"` overrides Alert's default `alert` — none of these is
// an interruption.
function StatusCard({
  bucket,
  title,
  children,
  actions,
}: {
  bucket: StatusBucket;
  title: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <Alert
      role="status"
      aria-live="polite"
      className="max-w-full bg-popover text-popover-foreground shadow-sm motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-1 motion-safe:duration-150"
    >
      <AlertTitle className={cn("flex items-center gap-2", bucketText[bucket])}>
        <StatusDot bucket={bucket} />
        {title}
      </AlertTitle>
      {children && <AlertDescription className="text-xs">{children}</AlertDescription>}
      {actions && <div className="mt-2 flex flex-wrap items-center gap-2">{actions}</div>}
    </Alert>
  );
}

export default function ServerStatusBanner() {
  const { banner, version, installedVersion, checkNow } = useServerStatus();
  if (banner === "hidden") return null;

  if (banner === "reconnected") {
    return <StatusCard bucket="green" title="Reconnected — fused-render is back." />;
  }

  if (banner === "update-refresh") {
    return (
      <StatusCard
        bucket="orange"
        title={`fused-render updated to v${version}`}
        actions={
          <Button type="button" variant="outline" size="sm" onClick={() => window.location.reload()}>
            Refresh page
          </Button>
        }
      >
        This page is still on v{BUILD_VERSION}. Refresh to load the new version.
      </StatusCard>
    );
  }

  if (banner === "update-restart") {
    return (
      <StatusCard
        bucket="orange"
        title={`fused-render v${installedVersion} is installed`}
        actions={
          /* fused-render://relaunch: the OS hands the link to the running app,
             which quits through the normal teardown and respawns from the
             bundle on disk. The down-card shows while it's gone, and the
             reconnect probe auto-reloads this page onto the new version. */
          <a className={buttonVariants({ variant: "outline", size: "sm" })} href="fused-render://relaunch">
            Restart fused-render
          </a>
        }
      >
        The app is still running v{version}. Restart fused-render to finish the update.
      </StatusCard>
    );
  }

  return (
    <StatusCard
      bucket="red"
      title="fused-render isn't running"
      actions={
        <>
          {/* fused-render://launch (D128): the OS starts the app, the server-boot
              makes the next probe succeed, and this page reconnects on its own —
              the link opens no tab and navigates nowhere. */}
          <a className={buttonVariants({ variant: "outline", size: "sm" })} href="fused-render://launch">
            Start fused-render
          </a>
          <Button type="button" variant="ghost" size="sm" onClick={checkNow}>
            Check again
          </Button>
        </>
      }
    >
      The app that powers this page has stopped or was closed. Reopen the fused-render app, and
      this page will reconnect on its own.
    </StatusCard>
  );
}
