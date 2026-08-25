// First-run onboarding tours (driver.js). One spotlight walkthrough per
// surface, each with its own seen key, each firing on the first visit to the
// route it is about — and all of them replayable from the sidebar's Tours menu.
// The registry and the pure helpers live in ./registry; this file is the driver
// runtime around them.
import { driver, type Driver, type DriveStep } from "driver.js";
import "driver.js/dist/driver.css";
import { IS_EMBED, navigateUrl } from "@platform/lib/router";
import { presentSteps, seenKey, type FlowStep, type FollowUp, type Tour } from "./registry";

export {
  TOURS,
  autoStartTourFor,
  tourById,
  type FlowStep,
  type FollowUp,
  type Tour,
} from "./registry";

function markSeen(id: string): void {
  try {
    localStorage.setItem(seenKey(id), "1");
  } catch {
    /* localStorage may be unavailable; the tour just replays next time */
  }
}

function hasSeen(id: string): boolean {
  try {
    return localStorage.getItem(seenKey(id)) === "1";
  } catch {
    return false;
  }
}

// The one live driver instance, shared across tours. runTour is a no-op while
// any tour is on screen, so a delayed auto-start can never stack on a manual
// replay (and vice versa).
let active: ReturnType<typeof driver> | null = null;

// A tour's follow-up (tasks: "now make one") continues INSIDE UI that only
// exists after the user acts — the New task modal. While the parent tour is on
// screen, one capture-phase click listener waits for the trigger; the click
// still reaches the app (the modal opens), the tour closes, and the follow-up
// steps run over the freshly mounted modal. One armed trigger at a time: a
// chained follow-up arms its own when the link before it starts.
let disarmFollowUp: (() => void) | null = null;

// An INTERACTIVE step (`advanceOn`) has no Next button: the tour waits for the
// user to do the thing — press Run in the playground composer — and the doing
// is what walks it on. Same capture-phase shape as the follow-up above (the
// click still reaches the app), except here the tour stays alive and steps
// forward instead of closing. One armed step at a time.
let disarmAdvance: (() => void) | null = null;

function armAdvance(step: FlowStep, d: Driver): void {
  disarmAdvance?.();
  const advance = () => {
    disarm();
    // A beat for the app to react before the spotlight moves: the reply block
    // is what the next step is about, and it mounts on this click.
    setTimeout(() => {
      if (d.isActive()) d.moveNext();
    }, 400);
  };
  const onClick = (e: MouseEvent) => {
    if (step.advanceOn && (e.target as Element | null)?.closest(step.advanceOn)) advance();
  };
  // The composer submits from its own onKeyDown — Enter never reaches the Run
  // button — so the keyboard path is watched on the box itself.
  const box = step.advanceOnEnter
    ? document.querySelector<HTMLElement>(step.advanceOnEnter)
    : null;
  const onKeyDown = (e: KeyboardEvent) => {
    // Enter submits from the composer's own onKeyDown without touching the Run
    // button, so it has to be watched here; it is let through untouched because
    // React's submit handler is bound above this element and still must see it.
    if (e.key === "Enter" && !e.shiftKey) advance();
  };
  const disarm = () => {
    document.removeEventListener("click", onClick, true);
    box?.removeEventListener("keydown", onKeyDown);
    if (disarmAdvance === disarm) disarmAdvance = null;
  };
  document.addEventListener("click", onClick, true);
  box?.addEventListener("keydown", onKeyDown);
  disarmAdvance = disarm;
}

// driver's arrow-key tour navigation is a WINDOW keyup listener with no focus
// guard: ArrowLeft while editing a prefilled prompt (or any modal field a tour
// is standing on) walks the tour instead of the caret — observed snapping the
// AI tour from its composer step back to step 1. While any tour is up, arrows
// whose target is an editable element stop here, at document capture, before
// the window ever hears them. Esc is untouched — closing must keep working.
function isEditable(t: EventTarget | null): boolean {
  return (
    t instanceof HTMLElement &&
    (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t.isContentEditable)
  );
}

function shieldArrows(e: KeyboardEvent): void {
  if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && isEditable(e.target)) {
    e.stopPropagation();
  }
}

// A tour is a held frame: the page must not slide out from under the spotlight.
// driver's own `allowScroll: false` freezes the BODY, but this app scrolls in
// inner containers (the folder listing, home's strips), which a wheel still
// reaches — so wheel and touch are stopped at capture unless they happen inside
// the spotlit element itself or the popover (the two places a tour step may
// still be asking the user to work in).
function blockScroll(e: Event): void {
  const t = e.target as Element | null;
  if (t?.closest(".driver-active-element, .driver-popover")) return;
  e.preventDefault();
}

function armScrollLock(): () => void {
  document.addEventListener("wheel", blockScroll, { capture: true, passive: false });
  document.addEventListener("touchmove", blockScroll, { capture: true, passive: false });
  return () => {
    document.removeEventListener("wheel", blockScroll, { capture: true });
    document.removeEventListener("touchmove", blockScroll, { capture: true });
  };
}

function armArrowShield(): () => void {
  // Both phases of the key: driver navigates on keyup, but shielding keydown
  // too keeps any similar listener from ever seeing an arrow meant for a caret.
  document.addEventListener("keydown", shieldArrows, true);
  document.addEventListener("keyup", shieldArrows, true);
  return () => {
    document.removeEventListener("keydown", shieldArrows, true);
    document.removeEventListener("keyup", shieldArrows, true);
  };
}

// Every step's own hooks, with the interactive extras folded in: `onEnter` on
// open (the prefill), the advance listener armed for as long as the step is up,
// and no Next button while it is — unless the control it is waiting for is not
// on screen, in which case the buttons stay and nobody is stranded.
function wireFlow(steps: FlowStep[]): DriveStep[] {
  return steps.map((s) => {
    const waits = !!s.advanceOn && !!document.querySelector(s.advanceOn);
    return {
      ...s,
      // An interactive step's Next is an ACTION button ("Run it", "Do it"): it
      // clicks the waited-for control for the user, and that synthetic click
      // walks through the same armAdvance listener a real one does — two ways
      // to the same place, matching the follow-up trigger's "Do it".
      popover: waits
        ? {
            ...s.popover,
            nextBtnText: s.actionText ?? "Do it",
            doneBtnText: s.actionText ?? "Do it",
            onNextClick: () => {
              // Deterministic: disarm FIRST so the synthetic click can't also
              // advance through the capture listener (double moveNext skips a
              // step), then click the control, then step on. Observed: relying
              // on the listener to catch the synthetic click worked on the
              // playground's Run but silently stalled on the tasks view
              // switcher, whose click re-renders the very segment it lives in.
              disarmAdvance?.();
              document.querySelector<HTMLElement>(s.advanceOn!)?.click();
              setTimeout(() => {
                if (active?.isActive()) active.moveNext();
              }, 400);
            },
          }
        : s.popover,
      onHighlighted: (el, step, opts) => {
        s.onEnter?.();
        if (waits) armAdvance(s, opts.driver);
        s.onHighlighted?.(el, step, opts);
      },
      onDeselected: (el, step, opts) => {
        disarmAdvance?.();
        s.onDeselected?.(el, step, opts);
      },
    } satisfies DriveStep;
  });
}

function armFollowUp(id: string, followUp: FollowUp): void {
  disarmFollowUp?.();
  const onClick = (e: MouseEvent) => {
    if (!(e.target as Element | null)?.closest(followUp.trigger)) return;
    disarm();
    // The same click may also be an interactive step's advanceOn (Create it is
    // both): kill that listener's pending moveNext HERE, not via onDestroyed —
    // driver skips onDestroyed in some environments, and a zombie moveNext 400ms
    // later would step the freshly chained one-step tour straight off its end.
    disarmAdvance?.();
    active?.destroy();
    // Wait for the revealed UI to mount before measuring it — and RETRY, not a
    // single beat: the click that triggers a link can also put the target list
    // through a refetch that unmounts its rows for a moment (observed: Create
    // closes the modal, the tasks list re-renders, and a one-shot 350ms check
    // lands exactly in the gap and the chain dies silently).
    let tries = 8;
    const tryStart = () => {
      const present = presentSteps(followUp.steps());
      if (present.length > 0) {
        // The chain: this link's own follow-up is handed to the tour it starts,
        // so an act inside the revealed UI can reveal the next one in turn.
        runTour(`${id}-flow`, present, followUp.followUp);
      } else if (--tries > 0) {
        setTimeout(tryStart, 350);
      }
    };
    setTimeout(tryStart, 350);
  };
  const disarm = () => {
    document.removeEventListener("click", onClick, true);
    if (disarmFollowUp === disarm) disarmFollowUp = null;
  };
  document.addEventListener("click", onClick, true);
  disarmFollowUp = disarm;
}

function runTour(id: string, flow: FlowStep[], followUp?: FollowUp): void {
  if (active?.isActive()) return;
  const steps = wireFlow(flow);
  // driver's default black 70% dim is near-invisible over the dark theme's
  // already-near-black surfaces — the spotlight has to carry the "look here",
  // so the dark overlay dims harder.
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  const d = driver({
    showProgress: true,
    allowClose: true,
    // No spotlight tween: driver defers each step's popover re-render to a
    // mid-animation rAF tick, and that loop stalls in this app (observed: a
    // step change moves the spotlight but leaves the popover display:none —
    // dimmed screen, no visible controls). With animate off the popover
    // renders synchronously on every step; driver's CSS fade still applies.
    animate: false,
    // The page holds still while a tour is up: driver locks the body scroll,
    // and the capture-phase wheel/touch lock below covers the app's inner
    // scroll containers.
    allowScroll: false,
    overlayOpacity: dark ? 0.82 : 0.7,
    steps,
    onDestroyed: () => {
      active = null;
      // A tour closed without its trigger being clicked takes its follow-up
      // with it — the listener must not outlive the invitation. Same for an
      // interactive step's advance listener: Esc on it ends the flow.
      disarmFollowUp?.();
      disarmAdvance?.();
      dropArrowShield();
      dropScrollLock();
    },
  });
  active = d;
  const dropArrowShield = armArrowShield();
  const dropScrollLock = armScrollLock();
  if (followUp) {
    armFollowUp(id, followUp);
    // The last step invites a click on the trigger, but "Done" must not be a
    // dead end — it clicks the trigger for the user, and that synthetic click
    // walks the same capture listener as a real one: modal opens, follow-up
    // steps run.
    const last = steps[steps.length - 1];
    last.popover = {
      ...last.popover,
      // An interactive last step already named its own action ("Create it",
      // from wireFlow) and that word is better than the generic one, so it is
      // only defaulted here rather than overwritten.
      doneBtnText: last.popover?.doneBtnText ?? "Do it",
      onNextClick: () => {
        const el = document.querySelector<HTMLElement>(followUp.trigger);
        if (el) el.click();
        else active?.destroy();
      },
    };
  }
  // Seen the moment it fires, not on teardown: driver's onDestroyed is gated on
  // internal animation state that never settles in every environment (observed:
  // it silently skips, letting the tour refire forever). Firing once IS the
  // "seen once" contract — abandoning counts as seeing it, and the replay menu
  // ignores the key anyway.
  markSeen(id);
  d.drive();
}

/** Manual replay (the sidebar's Tours menu): always runs, ignoring the seen
    key, using whatever of the tour's steps are currently on screen.

    Asked for from somewhere else — "AI Models" picked while reading Home — it
    goes to the tour's own page FIRST. The old shape simply found none of the
    tour's targets and returned, so the menu entry looked broken. The route hop
    is then polled for rather than waited out in one beat, the same reason
    armFollowUp retries: the new page's chrome mounts across several frames (and
    some of it after a fetch), and a single 350ms check lands in the gap. */
export function startTour(tour: Tour): void {
  if (!tour.matches(location.pathname)) {
    // startPath may need asking the server (explorer: whose home directory?) —
    // resolve it, then hop, then poll. A rejection just abandons the replay.
    void Promise.resolve(
      typeof tour.startPath === "function" ? tour.startPath() : tour.startPath
    ).then((path) => {
      navigateUrl(path);
      // Start only when two consecutive polls agree on a non-zero step count:
      // the new page's chrome mounts across several frames, and a poll that
      // fires between the breadcrumb and the pane runs a truncated walkthrough
      // (observed once: a 2-step explorer tour).
      let tries = 8;
      let lastCount = -1;
      const tryStart = () => {
        const steps = presentSteps(tour.steps());
        if (steps.length > 0 && steps.length === lastCount) {
          runTour(tour.id, steps, tour.followUp);
          return;
        }
        lastCount = steps.length;
        if (--tries > 0) setTimeout(tryStart, 350);
      };
      setTimeout(tryStart, 350);
    });
    return;
  }
  const steps = presentSteps(tour.steps());
  if (steps.length === 0) return;
  runTour(tour.id, steps, tour.followUp);
}

// First-run auto-start for one tour: only for a fresh, non-embed user with the
// expanded sidebar mounted. Called after paint so the route's own chrome
// exists. Returns true when there is nothing left to do (tour started, already
// seen, or embed) and false when the shell chrome simply isn't on screen yet —
// the caller retries on the next route change.
export function maybeAutoStartTour(tour: Tour): boolean {
  if (IS_EMBED) return true;
  if (hasSeen(tour.id)) return true;
  if (!document.querySelector("#sidebar")) return false;
  // A collapsed sidebar is a rail with none of the sidebar targets
  // (.sidebar-bookmarks, .sidebar-prefs-trigger) — starting there would run a
  // truncated walkthrough AND mark it seen. The brand row exists only in the
  // expanded frame, so it is the expansion check.
  if (!document.querySelector(".sidebar-brand")) return false;
  const steps = presentSteps(tour.steps());
  if (steps.length === 0) return false;
  runTour(tour.id, steps, tour.followUp);
  return true;
}
