// Persistent server-health banner (bottom-right, above the listing toast).
// Polls /api/config every 5s and shows a "server down" card after two
// consecutive failures — the backend is a native app the user launches, so
// the fix is always "reopen the app", not a CLI command. On recovery the
// card flips to a green "reconnected" state that auto-dismisses. Fully
// self-contained: mounted once in App's #app root so it survives the
// epoch-keyed view remounts. Styling is .server-status* in shell.css.
import { useEffect, useRef, useState } from "react";

const POLL_MS = 5000;
const PROBE_TIMEOUT_MS = 4000;
const FAIL_THRESHOLD = 2;
const RECONNECT_DISMISS_MS = 5000;

type Banner = "hidden" | "down" | "reconnected";

function useServerStatus(): { banner: Banner; checkNow: () => void } {
  const [banner, setBanner] = useState<Banner>("hidden");
  const failsRef = useRef(0);
  const probingRef = useRef(false);
  const probeRef = useRef<() => void>(() => {});
  const bannerRef = useRef<Banner>("hidden");
  bannerRef.current = banner;

  useEffect(() => {
    let disposed = false;
    let dismissTimer: number | undefined;

    async function probe() {
      if (probingRef.current) return;
      probingRef.current = true;
      let ok = false;
      const ctrl = new AbortController();
      const timeout = window.setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS);
      try {
        const res = await fetch("/api/config", { cache: "no-store", signal: ctrl.signal });
        ok = res.ok;
      } catch {
        ok = false;
      } finally {
        window.clearTimeout(timeout);
        probingRef.current = false;
      }
      if (disposed) return;
      if (ok) {
        failsRef.current = 0;
        if (bannerRef.current === "down") {
          setBanner("reconnected");
          window.clearTimeout(dismissTimer);
          dismissTimer = window.setTimeout(() => {
            if (!disposed) setBanner("hidden");
          }, RECONNECT_DISMISS_MS);
        }
      } else {
        failsRef.current += 1;
        if (failsRef.current >= FAIL_THRESHOLD) setBanner("down");
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

  return { banner, checkNow: () => probeRef.current() };
}

export default function ServerStatusBanner() {
  const { banner, checkNow } = useServerStatus();
  if (banner === "hidden") return null;

  if (banner === "reconnected") {
    return (
      <div className="server-status server-status-reconnected" role="status" aria-live="polite">
        Reconnected — fused-render is back.
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
      <button type="button" className="server-status-retry" onClick={checkNow}>
        Check again
      </button>
    </div>
  );
}
