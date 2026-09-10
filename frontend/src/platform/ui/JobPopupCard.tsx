// The floating pop-up half of a terminal job's notification (SPEC
// actionable-notifications, user: "when getting notifications, ensure the
// latest notification always pops up and auto disappears under 3 seconds.
// they still stay in the list"). It is the exact `JobRow` the Notifications
// panel draws for the same job — reused verbatim, the same way
// `shell/RepoUpdatesDock.tsx` reuses it for its own terminal rows — mounted
// here for `JOB_POPUP_VISIBLE_MS` and then playing `lib/toast`'s own
// grid-collapse exit (`TOAST_EXIT_MS`) before calling `onGone`. The row this
// job may also have in the panel (kept or not, per its tier — see `JobTier`
// in jobs.ts) is untouched either way: this card is a second, temporary way
// to see the SAME notification, never a second copy of it.
//
// CLICKING THE ROW opens `job.page` and dismisses it, exactly as `JobRow`'s
// own click handler always does — going to look is the acknowledgement, the
// same rule Notifications itself uses, so the panel row (if this job has
// one) really does clear.
//
// THE ✕ ONLY CLOSES THE CARD. It does not touch the panel: swatting away a
// pop-up is "I saw this, stop showing it to me", not "delete the
// Notifications row for it", so `onDismissClick` overrides `JobRow`'s
// ordinary ✕ to skip the real, server-side dismiss and just start this
// card's own exit animation instead. A job with nothing kept in the panel
// (a `transient` tier) loses nothing either way; a job that IS kept (an
// `attention`/`trail` row) stays there for the user to act on later — the
// one thing "they still stay in the list" requires.
//
// A PRESS ANYWHERE ELSE also starts the same exit — see the outside-press
// effect below for why that never disturbs the press itself.
import { useEffect, useRef, useState } from "react";
import { JobRow } from "@platform/ui/DownloadManager";
import { JOB_POPUP_VISIBLE_MS, type Job } from "@platform/lib/jobs";
import { TOAST_EXIT_MS } from "@platform/lib/toast";

const NOOP = () => {};

export default function JobPopupCard({
  job,
  onGone,
  cancelFn,
  dismissFn,
}: {
  job: Job;
  onGone: () => void;
  /** Test seam only, threaded straight through to `JobRow`'s own identical
   *  seam (JobPopupCard.test.tsx) — every real caller omits both and gets
   *  `JobRow`'s real `cancelJob`/`dismissJob` defaults. */
  cancelFn?: (id: string) => Promise<Job>;
  dismissFn?: (id: string) => Promise<{ dismissed: string }>;
}) {
  const [leaving, setLeaving] = useState(false);
  // Read by the exit timer without re-arming it on every render — `onGone`
  // is a fresh closure from `NotificationHost` on each of its own renders,
  // and this card must not restart its countdown just because its parent
  // re-rendered for an unrelated reason.
  const goneRef = useRef(onGone);
  goneRef.current = onGone;

  // `NotificationHost` mounts this with `key={job.id}` (App.tsx), so a new
  // job is always a fresh instance of this component — this effect runs
  // exactly once per card's whole life, never restarting mid-flight for the
  // same job.
  //
  // `globalThis.setTimeout`/`globalThis.clearTimeout`, not `window`'s — this
  // is the same exit-timing shape `lib/toast.ts` already documents at length:
  // a timer scheduled here through `window` fired inside a later bun test
  // file with no DOM shim installed, and `window is not defined` aborted the
  // whole run between files rather than failing the one test that owned it.
  useEffect(() => {
    const t = globalThis.setTimeout(() => setLeaving(true), JOB_POPUP_VISIBLE_MS);
    return () => globalThis.clearTimeout(t);
  }, []);

  useEffect(() => {
    if (!leaving) return;
    const t = globalThis.setTimeout(() => goneRef.current(), TOAST_EXIT_MS);
    return () => globalThis.clearTimeout(t);
  }, [leaving]);

  // A press anywhere else is "I saw this, move on" — the same acknowledgement
  // clicking the row already is, just aimed somewhere other than the row.
  // Neither `preventDefault` nor `stopPropagation` is ever called: whatever
  // the user actually pressed (a menu item, a link, another card) still gets
  // the event exactly as if this card were not here. Only the card's OWN
  // exit starts; the press itself is never disturbed.
  //
  // `click`, not `pointerdown`: a `pointerdown` fires before `pointerup`,
  // so starting the exit animation on it can unmount or reflow whatever the
  // user actually pressed before its own `click` ever fires — concretely, a
  // toast action button rendered ABOVE this card in `.notif-host` had its
  // click stolen by the card's own collapse. `click` fires only after the
  // full press-release, by which point the target's own handler has already
  // run, so the collapse can never race it.
  //
  // The "inside" check covers two things, not one: `cardRef.contains` for
  // this card's own row (its click handler already starts the same
  // `leaving` state, so it is left alone here rather than raced against),
  // and `closest(".notif-host")` for EVERYTHING ELSE the floating column
  // renders — another toast, the server-status banner, a toast's own action
  // button sitting above this card. A press on any of those is not "outside
  // the notification system", it is a press ON it, so it must not close a
  // sibling card.
  //
  // `globalThis.addEventListener`/`removeEventListener`, not `window`'s or
  // `document`'s — the same reason the timers above read `globalThis`:
  // `window`/`document` are no-op stubs in the test shim
  // (testDomShim.ts), while Bun's `globalThis` is a real `EventTarget`, so a
  // listener attached here is the one a test can actually exercise.
  const cardRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (leaving) return;
    const isInside = (target: Node | null) => {
      if (!target) return false;
      if (cardRef.current?.contains(target)) return true;
      const el = target as Node & { closest?: (sel: string) => Element | null };
      return !!el.closest?.(".notif-host");
    };
    const onOutside = (e: Event) => {
      if (isInside(e.target as Node | null)) return;
      setLeaving(true);
    };
    globalThis.addEventListener("click", onOutside, true);
    return () => globalThis.removeEventListener("click", onOutside, true);
  }, [leaving]);

  // App pages are hosted in iframes, so a press inside one never reaches
  // this document at all — no `click` this card can see. It DOES blur
  // whatever had focus here, though, so a narrow `blur` listener catches
  // exactly that case: `document.activeElement instanceof HTMLIFrameElement`
  // is true only when focus just left TO an iframe, never for alt-tabbing
  // away, opening devtools, or a native file picker, all of which blur the
  // window without handing focus to any iframe in it. Precedent:
  // `apps/explorer/BarMenu.tsx`'s `useMenuAnchor` closes on ANY window blur
  // unconditionally, which is right for a menu (any loss of focus should
  // close it) but wrong here (an alt-tab must not silently dismiss a card
  // the user hasn't acted on).
  useEffect(() => {
    if (leaving) return;
    const onBlur = () => {
      if (document.activeElement instanceof HTMLIFrameElement) setLeaving(true);
    };
    globalThis.addEventListener("blur", onBlur);
    return () => globalThis.removeEventListener("blur", onBlur);
  }, [leaving]);

  return (
    <div ref={cardRef} className={"toast-slot" + (leaving ? " leaving" : "")}>
      <JobRow
        job={job}
        onChanged={NOOP}
        onPatch={() => setLeaving(true)}
        onDismissClick={() => setLeaving(true)}
        cancelFn={cancelFn}
        dismissFn={dismissFn}
      />
    </div>
  );
}
