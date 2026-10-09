// Scheduled-message events → global toasts, off the events bus. Mounted ONCE
// at the app root (App), alongside useMountHealth, whose shape this follows.
//
// This exists because of the one thing that makes scheduled messages different
// from everything else the app runs: **nobody is looking when they happen.** A
// message that fired at 6am, or was missed because the app was closed, leaves a
// row on /tasks that is only ever seen by someone who goes to look. These
// toasts are what make "it ran"/"it's running"/"it didn't" arrive on their own.
//
// Rules (per event kind, SPEC-quiet-notifications.md §5):
//  - failed  → persistent, with an "Open" action onto /tasks. Covers both
//              halves of failing: the send never happened, or the turn it
//              started died. Either way a person has to decide something.
//              Never suppressed by presence.
//  - missed  → persistent, same action. Nothing went wrong — the app simply
//              wasn't running inside the catch-up window — but the user asked
//              for something that did not happen, so it still has to be said.
//              Never suppressed by presence.
//  - started → a suppressible info toast: skipped outright if the run's own
//              target is already open+focused in THIS window (`notify`'s own
//              `source` check), never retained. §5 reverses the old belief
//              that a run beginning is never worth saying — see
//              schedule-toast.ts's header for the writeup.
//  - done    → same shape as `started`. Also reverses this module's own old
//              "no toast at all" rule for `done` (D661,
//              DECISIONS-actionable-notifications.md).
//
// THE FEED IS A SUBSCRIPTION, NOT A POLL (Fused Events Bus, D3): the server
// pushes the `schedule.events` snapshot — the same body `GET
// /api/schedule/events` answered — on subscribe and again whenever an event is
// recorded. It is a NARRATOR topic (`hiddenOk: false`): a run that fires while
// this tab is hidden still has to be said, so the client keeps the
// subscription open through `document.hidden` instead of dropping it.
//
// NARRATOR-ONLY (§1/§5): only the elected top-level window narrates at all —
// every embed iframe, and every non-narrator top-level tab, would otherwise
// double/triple-toast the same events. Checked on every frame (not just once
// at mount) so narration hands off cleanly the moment the current narrator's
// tab closes and a heartbeat elects another.
import { useEffect, useRef } from "react";
import { ackScheduleEvents } from "@platform/lib/api";
import type { ScheduleEvent } from "@platform/lib/api";
import { subscribeTopic } from "@platform/lib/events";
import { IS_EMBED, navigateUrl } from "@platform/lib/router";
import { dismissNotification, dismissPopup, notify } from "@platform/lib/notifications";
import { isNarrator } from "@platform/lib/presence";
import { toastForEvent } from "@platform/lib/schedule-toast";
import type { ScheduleToast } from "@platform/lib/schedule-toast";

/**
 * @param onOutcome Called once per snapshot that narrated a started/done/failed
 *   event — a scheduled run's row just CHANGED (began running, or ended),
 *   which is the fact the Tasks page and the sidebar want on the same beat
 *   (their own listing subscription is resynced by it). The shell passes
 *   tasksPulse.pokeTasks here (App); it is a parameter rather than an import
 *   because that store lives in shell and platform may not reach up
 *   (frontend/scripts/check-boundaries.mjs). `missed` deliberately does not
 *   fire it: nothing ran, so no row is mid-flip anywhere.
 */
export function useScheduleEvents(onOutcome?: () => void): void {
  // The highest event id already turned into a toast IN THIS PAGE. A ref (not
  // state) so it survives re-renders without re-subscribing, and so two
  // snapshots landing back to back can't narrate the same event twice while
  // the ack for the first is still in flight.
  //
  // There is deliberately **no silent baseline** here, unlike useMountHealth.
  // The server only hands over events nobody has confirmed narrating
  // (`/api/schedule/events` + the ack below), so a reload is quiet without the
  // client having to guess — and, the part that matters, a catch-up `missed`
  // verdict emitted by the scheduler's first tick still gets said out loud when
  // the shell finally loads. A client-side baseline swallowed exactly those.
  const lastEventId = useRef(0);
  // Through a ref so the subscription below (opened once, deps []) always calls
  // the caller's CURRENT function rather than the one from the mounting render.
  const outcome = useRef(onOutcome);
  outcome.current = onOutcome;

  useEffect(() => {
    // Only the top-level shell narrates: every embed iframe would otherwise
    // subscribe and double-toast the same events into the host page.
    if (IS_EMBED) return;
    let alive = true;

    const narrate = async (body: { events: ScheduleEvent[] }) => {
      // Re-checked on every frame, not just once at mount — see header comment.
      if (!isNarrator()) return;
      if (!alive) return;

      const fresh = body.events.filter((e) => e.id > lastEventId.current);
      if (fresh.length === 0) return;
      const highest = Math.max(...fresh.map((e) => e.id));
      lastEventId.current = Math.max(lastEventId.current, highest);

      for (const e of fresh) push(toastForEvent(e));
      // The events just narrated are also the earliest word this page has that
      // a run's STATE changed (started running, or ended) — see the onOutcome
      // contract above. Once per batch, not per event: the outcome callback
      // resyncs the listing, and one resync reads them all.
      if (fresh.some((e) => e.kind === "started" || e.kind === "done" || e.kind === "failed")) {
        outcome.current?.();
      }
      // Confirm only AFTER narrating: a page that dies in between sees these
      // once more, which is a duplicate toast rather than a silent miss — the
      // right way round for the one thing here that must not go unsaid. This
      // is also what makes an unattended run's notification unlosable: nobody
      // narrating (no narrator tab open at all) means nothing acks, so the
      // event is still sitting in the snapshot, undelivered, the next time any
      // window subscribes — suppression only ever means "already seen", never
      // "nobody was there" (SPEC-quiet-notifications.md §5's own named trap).
      try {
        await ackScheduleEvents(highest);
      } catch {
        // The local mark already stops this page repeating them; the server will
        // simply offer them again to the next one.
      }
    };

    // The rules live in `toastForEvent`. `started`/`done` (`tone: "info"`)
    // carry no action — `notify`'s own presence check (`source`) is what
    // suppresses them when the run's own target is already open+focused
    // here, and they are never retained (no action/page). `failed`/`missed`
    // (`tone: "error"`) keep the old persistent shape: never suppressed
    // (no `source`), always retained, with an "Open" action onto the page
    // whose row carries the reason, the target, and the transcript's run id.
    const push = (t: ScheduleToast) => {
      if (t.tone === "info") {
        notify({ title: t.msg, tone: "info", source: t.source });
        return;
      }
      const id = notify({
        title: t.msg,
        tone: "error",
        action: {
          label: "Open",
          onClick: () => {
            dismissPopup(id);
            dismissNotification(id);
            navigateUrl("/tasks");
          },
        },
      });
    };

    // `hiddenOk: false` — the narrator topic: the client never drops this
    // subscription while the tab is hidden, which is exactly when an
    // unattended run fires. An `err` frame (the GET's refusal) carries no
    // snapshot and is simply skipped; the next push is the retry.
    const off = subscribeTopic<{ events: ScheduleEvent[] }>(
      "schedule.events",
      null,
      (snap) => {
        if (snap) void narrate(snap);
      },
      { hiddenOk: false },
    );
    return () => {
      alive = false;
      off();
    };
  }, []);
}
