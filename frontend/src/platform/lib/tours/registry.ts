// The tour registry: one entry per surface, plus the pure helpers the runtime
// (./index.ts) and the tests share. DOM-free and driver-free on purpose — the
// matching, the seen-key gating and the step filtering are the parts worth
// testing, and none of them needs a document.
import type { DriveStep } from "driver.js";
import { homeTour } from "./home";
import { tasksTour } from "./tasks";
import { aiTour } from "./ai";
import { explorerTour } from "./explorer";

/** A driver step plus the three things an INTERACTIVE step needs: something to
    do when it opens, the click that ends it, and the keystroke that means the
    same click. All three are read by the runtime (./index.ts) — this file stays
    DOM-free, so `onEnter` is only ever STORED here, never called. */
export type FlowStep = DriveStep & {
  /** CSS selector for the control this step is asking the user to click. The
      step drops its Next button — the click itself walks the tour on. */
  advanceOn?: string;
  /** CSS selector for a text box where Enter does what `advanceOn` does. The
      playground composer submits straight from its own onKeyDown without ever
      touching its Run button, so the keyboard path needs saying out loud. */
  advanceOnEnter?: string;
  /** Label for the popover's action button on an `advanceOn` step — it clicks
      the control for the user, exactly like the follow-up's "Do it". Defaults
      to "Do it". */
  actionText?: string;
  /** Run when the step opens — the prefill hook. */
  onEnter?: () => void;
};

/** A do-it continuation: while the tour it belongs to is on screen, a click on
    `trigger` closes that tour and walks `steps` over the UI the click revealed
    (tasks: the New task modal). Steps are filtered to what actually mounted,
    like any tour's.

    CHAINED, because one act reveals the next: the follow-up may carry its own
    follow-up, so pressing Create inside the modal hands the walkthrough on to
    the list the created task lands in. Each link runs as its own tour (id
    `<parent>-flow`), so the whole chain keeps one live driver. */
export interface FollowUp {
  trigger: string;
  steps(): FlowStep[];
  followUp?: FollowUp;
}

export interface Tour {
  /** Stable id — half of the localStorage seen key, so renaming one replays
      the tour for everyone. */
  id: string;
  /** Human name, shown in the sidebar's replay menu. */
  title: string;
  /** Is this tour about the given route? */
  matches(pathname: string): boolean;
  /** Where a replay has to be standing for the tour's steps to exist. A manual
      replay from a route this tour is not about navigates here first (the
      runtime does it — this file stays DOM-free and router-free), so picking
      "AI Models" from the sidebar on Home takes you to the page it is about
      instead of finding none of its targets and silently doing nothing. A plain
      string where one exists — the sidebar's own AI Models row uses
      `tabHref("playground", "")`, and a tour reaching into an app's route
      helpers would put an @apps import in the platform layer. A FUNCTION when
      no fixed path can be written down: the explorer tour needs a real folder
      view, and whose home directory that is only the server knows. */
  startPath: string | (() => string | Promise<string>);
  steps(): FlowStep[];
  /** False for a tour that only ever runs from the replay menu (explorer):
      its steps are chrome a returning user already knows, and firing it on a
      first folder view would spend the one first-run moment on the listing. */
  autoStart?: boolean;
  /** Auto-start holds off while this is false — for a page whose chrome is up
      but whose CONTENT is still loading (home: skeleton cards). Without it the
      tour fires with the loading-dependent steps filtered out, marks itself
      seen, and those steps are skipped forever. The caller retries. */
  readyWhen?: () => boolean;
  /** A do-it continuation, possibly chained — see FollowUp. */
  followUp?: FollowUp;
}

// Order is the replay menu's order: the three auto-firing surface tours in the
// order a new user meets them, then the explorer walkthrough.
export const TOURS: Tour[] = [homeTour, tasksTour, aiTour, explorerTour];

export function tourById(id: string): Tour | null {
  return TOURS.find((t) => t.id === id) ?? null;
}

/** The tour that should fire on a first visit to `pathname`, if any. */
export function autoStartTourFor(pathname: string): Tour | null {
  return TOURS.find((t) => t.autoStart !== false && t.matches(pathname)) ?? null;
}

// v2 keys are deliberately fresh: the old single `fused.tour.seen` said nothing
// about which of these four a user has met, so existing users get each new tour
// once rather than none of them.
export function seenKey(id: string): string {
  return `fused.tour.v2.${id}`;
}

/** Steps whose target is on screen right now. A tour never breaks on a step
    whose element is conditionally hidden (embed mode, a live search that
    unmounts Home's strips, a collapsed panel) — the step just drops out. */
export function presentSteps<T extends DriveStep>(
  steps: T[],
  has: (selector: string) => boolean = (s) => !!document.querySelector(s),
): T[] {
  return steps.filter((s) => typeof s.element === "string" && has(s.element));
}
