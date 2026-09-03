// Background scheduled-message poll → global toasts. Mounted ONCE at the app
// root (App), alongside useMountHealth, whose shape this follows exactly.
//
// This exists because of the one thing that makes scheduled messages different
// from everything else the app runs: **nobody is looking when they happen.** A
// message that fired at 6am, or was missed because the app was closed, leaves a
// row on /tasks that is only ever seen by someone who goes to look. These
// toasts are what make "it ran" and "it didn't" arrive on their own.
//
// Rules (per event kind):
//  - failed  → error, persistent, with an "Open" action onto /tasks. Covers
//              both halves of failing: the send never happened, or the turn it
//              started died. Either way a person has to decide something.
//  - missed  → error, persistent, same action. Nothing went wrong — the app
//              simply wasn't running inside the catch-up window — but the user
//              asked for something that did not happen, so it is not an info.
//  - done    → info, auto-dismissing. Your message ran; no decision to make.
//  - attention → info, persistent, with an "Open" action onto the CHAT ITSELF.
//              The turn has raised a permission or question card and nobody is
//              there to answer it — for an unattended run, the single most
//              likely way to be stuck. Info because nothing has gone wrong;
//              persistent because the ask does not expire; onto the thread
//              because that is where the card the person has to answer is.
import { useEffect, useRef } from "react";
import { ackScheduleEvents, getScheduleEvents } from "@platform/lib/api";
import { IS_EMBED, navigateUrl } from "@platform/lib/router";
import { dismissToast, pushToast } from "@platform/lib/toast";
import { toastForEvent } from "@platform/lib/schedule-toast";
import type { ScheduleToast } from "@platform/lib/schedule-toast";

const POLL_MS = 15_000;

/**
 * @param onOutcome Called once per poll that narrated a done/failed event — a
 *   scheduled run just ENDED, which is exactly the fact the Tasks page and the
 *   sidebar are otherwise waiting out their own timers to learn. The shell
 *   passes tasksPulse.pokeTasks here (App); it is a parameter rather than an
 *   import because that store lives in shell and platform may not reach up
 *   (frontend/scripts/check-boundaries.mjs). `missed` deliberately does not
 *   fire it: nothing ran, so no row is mid-flip anywhere. `attention` DOES: no
 *   run ended, but a row just changed status, and it changed into the one status
 *   the page exists to show first.
 * @param chatHref Builds the URL of one conversation — shell's
 *   `schedule-lib.explorerUrl`, handed in for the same boundary reason as
 *   `onOutcome`. Only an `attention` toast has anywhere finer than /tasks to go,
 *   and without this (or without a session id yet) that is where it goes.
 */
export function useScheduleEvents(
  onOutcome?: () => void,
  chatHref?: (target: string, sessionId: string) => string,
): void {
  // The highest event id already turned into a toast IN THIS PAGE. A ref (not
  // state) so it survives re-renders without re-arming the interval, and so two
  // overlapping polls can't narrate the same event twice while the ack for the
  // first is still in flight.
  //
  // There is deliberately **no silent baseline** here, unlike useMountHealth.
  // The server only hands over events nobody has confirmed narrating
  // (`/api/schedule/events` + the ack below), so a reload is quiet without the
  // client having to guess — and, the part that matters, a catch-up `missed`
  // verdict emitted by the scheduler's first tick still gets said out loud when
  // the shell finally loads. A client-side baseline swallowed exactly those.
  const lastEventId = useRef(0);
  // Through a ref so the poll loop below (armed once, deps []) always calls the
  // caller's CURRENT function rather than the one from the mounting render.
  const outcome = useRef(onOutcome);
  outcome.current = onOutcome;
  // Same reason, same shape: the loop is armed once and must call the CURRENT
  // builder rather than the one from the mounting render.
  const href = useRef(chatHref);
  href.current = chatHref;

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
      if (fresh.length === 0) return;
      const highest = Math.max(...fresh.map((e) => e.id));
      lastEventId.current = Math.max(lastEventId.current, highest);

      for (const e of fresh) push(toastForEvent(e));
      // The events just narrated are also the earliest word this poll has that
      // a run ENDED — see the onOutcome contract above. Once per batch, not per
      // event: the outcome callback refetches, and one refetch reads them all.
      if (fresh.some((e) => e.kind === "done" || e.kind === "failed"
                            || e.kind === "attention")) {
        outcome.current?.();
      }
      // Confirm only AFTER narrating: a page that dies in between sees these
      // once more, which is a duplicate toast rather than a silent miss — the
      // right way round for the one thing here that must not go unsaid.
      try {
        await ackScheduleEvents(highest);
      } catch {
        // The local mark already stops this page repeating them; the server will
        // simply offer them again to the next one.
      }
    };

    // The rules live in `toastForEvent`; this only turns one into a real toast.
    // The action on an attention-needing one opens the page that can explain it —
    // the row there carries the reason, the target, and the transcript's run id.
    const push = (t: ScheduleToast) => {
      if (!t.needsAttention) {
        pushToast({ msg: t.msg, tone: t.tone });
        return;
      }
      // The thread when the event named one and the shell handed a builder in
      // (an `attention` toast, whose whole point is landing ON the card), /tasks
      // otherwise — the page that can explain anything else, and the honest
      // fallback for a run whose session id the watcher has not learnt yet.
      // The TARGET has to be there, not just the session: the chat is opened by
      // opening the folder it happened in, and a URL built on an empty path is a
      // link to nowhere rather than a link to the thread.
      const to = t.open && t.open.target && href.current
        ? href.current(t.open.target, t.open.sessionId)
        : "/tasks";
      const id = pushToast({
        msg: t.msg,
        tone: t.tone,
        ttlMs: 0, // persist until acted on / dismissed
        action: {
          label: "Open",
          onClick: () => {
            dismissToast(id);
            navigateUrl(to);
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
