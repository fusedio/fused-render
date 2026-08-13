// Background scheduled-message poll → global toasts. Mounted ONCE at the app
// root (App), alongside useMountHealth, whose shape this follows exactly.
//
// This exists because of the one thing that makes scheduled messages different
// from everything else the app runs: **nobody is looking when they happen.** A
// message that fired at 6am, or was missed because the app was closed, leaves a
// row on /scheduled that is only ever seen by someone who goes to look. These
// toasts are what make "it ran" and "it didn't" arrive on their own.
//
// Rules (per event kind):
//  - failed  → error, persistent, with an "Open" action onto /scheduled. Covers
//              both halves of failing: the send never happened, or the turn it
//              started died. Either way a person has to decide something.
//  - missed  → error, persistent, same action. Nothing went wrong — the app
//              simply wasn't running inside the catch-up window — but the user
//              asked for something that did not happen, so it is not an info.
//  - done    → info, auto-dismissing. Your message ran; no decision to make.
import { useEffect, useRef } from "react";
import { getScheduleEvents } from "@platform/lib/api";
import { IS_EMBED, navigateUrl } from "@platform/lib/router";
import { dismissToast, pushToast } from "@platform/lib/toast";
import { toastForEvent } from "@platform/lib/schedule-toast";
import type { ScheduleToast } from "@platform/lib/schedule-toast";

const POLL_MS = 15_000;

export function useScheduleEvents(): void {
  // The highest event id already turned into a toast. A ref (not state) so it
  // survives re-renders without re-arming the interval, and so overlapping polls
  // can't re-narrate the same event. -1 means "no baseline yet".
  const lastEventId = useRef(-1);
  // The FIRST successful poll is a silent baseline — opening the app must not
  // replay a backlog of last week's outcomes as toasts. Tied to success, not to
  // the first attempt, so a failed initial read doesn't leave the mark at -1.
  const baselined = useRef(false);

  useEffect(() => {
    // Only the top-level shell narrates: every embed iframe would otherwise poll
    // and double-toast the same events into the host page.
    if (IS_EMBED) return;
    let alive = true;

    const poll = async () => {
      let body;
      try {
        body = await getScheduleEvents();
      } catch {
        return; // network blip / server restart — retry next interval
      }
      if (!alive) return;

      const fresh = body.events.filter((e) => e.id > lastEventId.current);
      lastEventId.current = Math.max(lastEventId.current, ...fresh.map((e) => e.id));
      const isBaseline = !baselined.current;
      baselined.current = true;
      if (fresh.length === 0) return;
      if (isBaseline) return; // baseline pass — mark seen, stay silent

      for (const e of fresh) push(toastForEvent(e));
    };

    // The rules live in `toastForEvent`; this only turns one into a real toast.
    // The action on an attention-needing one opens the page that can explain it —
    // the row there carries the reason, the target, and the transcript's run id.
    const push = (t: ScheduleToast) => {
      if (!t.needsAttention) {
        pushToast({ msg: t.msg, tone: t.tone });
        return;
      }
      const id = pushToast({
        msg: t.msg,
        tone: t.tone,
        ttlMs: 0, // persist until acted on / dismissed
        action: {
          label: "Open",
          onClick: () => {
            dismissToast(id);
            navigateUrl("/scheduled");
          },
        },
      });
    };

    poll();
    const timer = window.setInterval(poll, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);
}
