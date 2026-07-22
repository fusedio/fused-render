// Background mount-health poll → global toasts. Mounted ONCE at the app root
// (App): every ~15s (and once on mount) it reads /api/mounts/health and turns
// NEW events into toasts. The backend owns DETECTION only — auto-reconnect is
// off (it churned on flap-prone mounts), so the user repairs a drop manually.
//
// Rules (per event kind):
//  - disconnected     → error "<name> disconnected", persistent, with a manual
//                       "Reconnect" action (reconnectMount + re-poll). This is
//                       the only kind the backend monitor emits today.
//  - reconnected      → info  "<name> reconnected" (defensive; not emitted by
//                       the detection-only monitor, kept for forward-compat).
import { useEffect, useRef } from "react";
import { getMountsHealth, reconnectMount } from "./api";
import { IS_EMBED } from "./router";
import { dismissToast, pushToast } from "./toast";

const POLL_MS = 15_000;

export function useMountHealth(): void {
  // The highest event id already turned into a toast. A ref (not state) so it
  // survives re-renders without re-arming the interval, and so overlapping
  // polls can't re-narrate the same event. -1 means "no baseline yet".
  const lastEventId = useRef(-1);

  useEffect(() => {
    // Only the top-level shell narrates mount health — every embed iframe would
    // otherwise poll and double-toast the same events into the host page.
    if (IS_EMBED) return;
    let alive = true;

    // A single poll. `showToasts` is false for the very first read so we
    // establish the high-water mark against events that predate this session
    // (a page load must not replay a backlog of old disconnects).
    const poll = async (showToasts: boolean) => {
      let health;
      try {
        health = await getMountsHealth();
      } catch {
        return; // network blip / server restart — skip, retry next interval
      }
      if (!alive) return;

      // New events only: id strictly above the mark. The log is append-only
      // and monotonic, so this both dedups and orders naturally.
      const fresh = health.events.filter((e) => e.id > lastEventId.current);
      if (fresh.length === 0) return;
      lastEventId.current = Math.max(lastEventId.current, ...fresh.map((e) => e.id));
      if (!showToasts) return; // baseline pass — mark seen, stay silent

      for (const e of fresh) {
        if (e.kind === "disconnected") {
          pushMountDisconnected(e.mount_id, e.name);
        } else if (e.kind === "reconnected") {
          pushToast({ msg: `${e.name} reconnected`, tone: "info" });
        }
      }
    };

    // A persistent error toast whose "Reconnect" action repairs the mount and
    // re-polls; success/failure each raise their own follow-up toast.
    const pushMountDisconnected = (mountId: string, name: string) => {
      const id = pushToast({
        msg: `${name} disconnected`,
        tone: "error",
        ttlMs: 0, // persist until acted on / dismissed
        action: {
          label: "Reconnect",
          onClick: async () => {
            dismissToast(id);
            try {
              await reconnectMount(mountId);
              pushToast({ msg: `${name} reconnected`, tone: "info" });
            } catch (err) {
              pushToast({
                msg: `${name} — reconnect failed: ${(err as Error).message}`,
                tone: "error",
              });
            }
            // Re-poll either way so the log's follow-up events (and any other
            // mounts' state) converge without waiting out the interval.
            void poll(true);
          },
        },
      });
    };

    void poll(false); // baseline on mount — no toast replay
    const timer = window.setInterval(() => void poll(true), POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);
}
