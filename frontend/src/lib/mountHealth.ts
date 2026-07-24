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
  // Whether a successful read has established the high-water mark yet. The
  // FIRST successful poll is the silent baseline — tied to success, not to the
  // first attempt, so a failed initial read doesn't leave the mark at -1 and
  // make the next poll replay the whole backlog as toasts.
  const baselined = useRef(false);

  useEffect(() => {
    // Only the top-level shell narrates mount health — every embed iframe would
    // otherwise poll and double-toast the same events into the host page.
    if (IS_EMBED) return;
    let alive = true;

    // A single poll. The first read that SUCCEEDS establishes the high-water
    // mark silently (a page load must not replay a backlog of old disconnects);
    // every read after that toasts genuinely new events.
    const poll = async () => {
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
      lastEventId.current = Math.max(lastEventId.current, ...fresh.map((e) => e.id));
      const isBaseline = !baselined.current;
      baselined.current = true;
      if (fresh.length === 0) return;
      if (isBaseline) return; // baseline pass — mark seen, stay silent

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
            void poll();
          },
        },
      });
    };

    void poll(); // first success is the silent baseline (see `baselined`)
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);
}
