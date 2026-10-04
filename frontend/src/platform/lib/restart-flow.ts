// What a restart LOOKS LIKE from a page that asked for one. Pure — the store
// (restart-store.ts) owns the channel, the timers and the deep link; this owns
// what one probe result means while the app is quitting and coming back.
//
// THE CLICK IS THE ONLY RELIABLE SIGNAL. `fused-render://relaunch` is handed to
// the OS: it returns nothing to the page, and the server has no "quitting on
// purpose" flag to poll — `should_exit` is the first step of `quit_teardown`
// (`fused_render/app.py`), so a 5 s poll would miss the window entirely and a
// flag added there would be a lie more often than not. So the page starts the
// clock itself, on the press, and reads the outage that follows as the restart
// it just asked for rather than as the app having died.
//
// The stages are what that outage looks like in order:
//   ready        — nothing asked for; the dialog shows its button.
//   quitting     — the press landed; the server is still answering (teardown
//                  takes seconds: `quit_teardown` logs its own duration).
//   restarting   — the first probe failed; the process is gone.
//   reconnecting — it is still gone after a second failure; the respawn is
//                  what we are waiting on now.
//   back         — a probe answered again on a DIFFERENT version than the one
//                  that was running when the button was pressed. Nothing else
//                  counts: a healthy answer on the same version is the old
//                  process still up, and a body with no version in it says
//                  nothing about which process answered.
//   gave-up      — RESTART_GIVE_UP_MS passed without it coming back. The
//                  restart story ends here: a stage label held forever is a
//                  promise the page cannot keep, and every stage draws a dialog
//                  with no ✕, no Esc and no backdrop. What replaces it is
//                  whatever the SERVER says — the ordinary "down" card while it
//                  is still not answering, the dialog with its button live
//                  again if it is (see `bannerSurface`).
//
// `back` does NOT reload the page from here. `reduceProbe` (server-status.ts)
// already returns `reload: true` for exactly this transition — a version that
// moved across a down gap — and the component already acts on it. Two reloads
// for one event is one reload too many; this stage only names what is on
// screen for the moment before that one lands.

export const RESTART_STAGES = [
  "ready",
  "quitting",
  "restarting",
  "reconnecting",
  "back",
  "gave-up",
] as const;

export type RestartStage = (typeof RESTART_STAGES)[number];

export interface RestartState {
  stage: RestartStage;
  /** Epoch ms of the press that started this, or null when nothing is in
   *  flight. Shared verbatim across windows (D3) so every window's cap
   *  expires at the same instant rather than at its own. */
  requestedAt: number | null;
  /** Consecutive failed probes since the press. */
  fails: number;
  /** The version the server was serving when the button was pressed — what a
   *  later healthy probe is compared against to know the process swapped. */
  before: string | null;
}

/** The cap (D4). Two minutes: a teardown plus a cold start of a signed bundle
 *  runs about a minute in practice (Akshil, 2026-09-19), so the cap sits at
 *  double that — long enough never to fire on a restart that is going fine,
 *  short enough that one that is never coming back hands the page to the
 *  "down" card while the reader is still watching. */
export const RESTART_GIVE_UP_MS = 120_000;

/** How many failed probes in a row read as "still gone" rather than "just
 *  went". Deliberately the same shape as `FAIL_THRESHOLD` next door, and
 *  deliberately its own constant: this one is about wording a wait, that one
 *  is about declaring an outage. */
export const RESTART_RECONNECTING_FAILS = 2;

export type RestartEvent =
  /** The press. `at` is carried rather than taken from the clock so a window
   *  that LATCHED someone else's press runs the same cap as the one that made
   *  it (D3), instead of starting a fresh minute of its own. */
  | { type: "request"; at: number; served?: string | null }
  | { type: "probe"; ok: boolean; version?: string | null }
  /** The clock alone. The cap must be able to fire with no probe landing —
   *  probes do keep coming during an outage, but nothing in the reducer should
   *  depend on that to keep a promise about time. */
  | { type: "tick" };

export function initialRestart(): RestartState {
  return { stage: "ready", requestedAt: null, fails: 0, before: null };
}

/** Whether this stage is a restart the page is currently narrating — the one
 *  question both the modal (show it) and the banner (hide the "down" card,
 *  step 5) ask, so they cannot disagree about it. `back` counts: the reload is
 *  a beat away and the down card must not flash in between. */
export function restartInFlight(stage: RestartStage): boolean {
  return stage === "quitting" || stage === "restarting" || stage === "reconnecting" || stage === "back";
}

export function reduceRestart(state: RestartState, event: RestartEvent, now: number): RestartState {
  if (event.type === "request") {
    // Re-armable from ANY stage, `gave-up` included: the cap dropped the story,
    // it did not forbid asking again, and the down card the cap falls through
    // to is a surface with its own way back.
    return { stage: "quitting", requestedAt: event.at, fails: 0, before: event.served ?? null };
  }

  // Nothing in flight, or a story that has already ended.
  if (state.requestedAt === null) return state;
  if (state.stage === "ready" || state.stage === "gave-up") return state;

  // THE CAP WINS FROM EVERY STAGE, `back` INCLUDED, and it is checked before
  // anything else so that is a property of the reducer rather than a promise
  // repeated on five branches. No stage may outlive `RESTART_GIVE_UP_MS`: every
  // one of them draws a dialog with no ✕, no Esc and no backdrop, so a stage
  // that cannot expire is a page with no way out. `back` is terminal against
  // PROBES (below) but not against the clock — it is normally a beat away from
  // `reduceProbe`'s own reload, and if that reload does not come (a second
  // update landing while the first was restarting leaves the disk still ahead,
  // so `reduceProbe` returns `update-restart` and no reload at all) the cap is
  // the only thing left to unstick it.
  if (now - state.requestedAt > RESTART_GIVE_UP_MS) return { ...state, stage: "gave-up" };

  // Reached `back` inside the window: the restart is over, and nothing a later
  // probe says re-opens the wait.
  if (state.stage === "back") return state;

  if (event.type === "probe" && event.ok) {
    // A VERSION THAT MOVED IS THE ONLY PROOF THE PROCESS SWAPPED, and this used
    // to also accept "any answer at all after the server had stopped
    // answering". That second rule was wrong in the exact case this flow is
    // about: a teardown takes seconds, so ONE probe can time out while the
    // socket is still open (a blip, a slow quit, a machine under load) and the
    // NEXT one answers — from the OLD process, which has not gone anywhere. The
    // weak rule read that as success and latched `back`, a terminal stage, on a
    // dialog with no button and no way to close it (bugbot, PR #1214, HIGH).
    //
    // So: the version, or nothing. `before` is what the server was serving when
    // the button was pressed; a different one can only come from a different
    // process.
    const moved =
      typeof event.version === "string" &&
      event.version !== "" &&
      state.before !== null &&
      event.version !== state.before;
    if (moved) return { ...state, stage: "back", fails: 0 };

    // A healthy answer with NO version in it is INCONCLUSIVE — it says the
    // socket is open, not which process owns it. Nothing moves, including the
    // failure count: resetting that would be a claim this probe cannot make.
    // The cap above still runs, so an endless run of these ends in `gave-up`
    // rather than in a stage held forever.
    if (typeof event.version !== "string" || event.version === "" || state.before === null) {
      return state;
    }

    // A healthy answer on the SAME version: the old process is still up, which
    // is what "Quitting…" means — so the stage goes BACK to it rather than
    // forward. Honest in both directions: a wait that had reached
    // "Reconnecting…" on a blip is not allowed to keep claiming the app is gone
    // when it is demonstrably answering, and the failure count resets so a real
    // outage after this re-walks the stages from the start. The cap is
    // untouched by any of it, so a press the app never acted on — which looks
    // exactly like this, forever — still ends.
    return { ...state, stage: "quitting", fails: 0 };
  }

  if (event.type === "probe") {
    const fails = state.fails + 1;
    const stage: RestartStage = fails >= RESTART_RECONNECTING_FAILS ? "reconnecting" : "restarting";
    return { ...state, stage, fails };
  }

  return state;
}

/** ONE WORD AND AN ELLIPSIS for the three waits (Akshil, 2026-09-08: "no
 *  longer phrases, just words") — the same vocabulary `UpdateBadge` uses for
 *  "Installing…"/"Downloading…". The end is the exception and is not a stage
 *  word at all: it is the sentence the reconnected pill has always said, said
 *  here because the modal is what is on screen at that moment. `ready` and
 *  `gave-up` have no label — neither is a wait, and both hand the surface to
 *  something else (the button, the down card). */
export function restartStageLabel(stage: RestartStage): string {
  if (stage === "quitting") return "Quitting…";
  if (stage === "restarting") return "Restarting…";
  if (stage === "reconnecting") return "Reconnecting…";
  if (stage === "back") return "Reconnected — fused-render is back.";
  return "";
}

// ---- the three-step strip --------------------------------------------------
// WHAT THE WAIT LOOKS LIKE, not just what it is called. One word replacing
// another said nothing about how far along the restart was: "Restarting…" on
// its own could be the first second or the fiftieth, and the one question a
// reader of a page-blocking dialog has is "is this getting anywhere". Three
// named steps with a position in them answer it without a progress bar, which
// would have to invent a percentage nothing here knows.
//
// The strip is derived from the stage rather than accumulated, so a REGRESSION
// is free: `reduceRestart` sends `reconnecting` back to `quitting` when the old
// process answers on the same version (the blip), and the strip simply un-ticks
// — there is no "furthest reached" to unwind and nothing animates backwards.
//
// `back` and `gave-up` are not steps. `back` is every step done; `gave-up` is
// the strip with no claim left to make, which is why it shows no live step at
// all rather than freezing a spinner on the one it died in — a spinner held
// forever is the promise the cap exists to stop making.

/** The three waits, in order. Deliberately the STAGE names: the strip cannot
 *  drift from the machine if it is indexed by it. */
export const RESTART_STEP_STAGES = ["quitting", "restarting", "reconnecting"] as const;

export type RestartStepStage = (typeof RESTART_STEP_STAGES)[number];

/** ONE WORD, no ellipsis — the step's NAME, which is a thing rather than an
 *  action in progress (the ellipsis belongs to `restartStageLabel`, which is
 *  what the LIVE step says instead). Same vocabulary either way: the live word
 *  is visibly the tense of the name next to it. */
export function restartStepWord(stage: RestartStepStage): string {
  if (stage === "quitting") return "Quit";
  if (stage === "restarting") return "Restart";
  return "Reconnect";
}

export type RestartStepState = "done" | "live" | "upcoming";

export interface RestartStep {
  stage: RestartStepStage;
  /** The name when the step is done or still to come; the `…` label while it
   *  is the one being waited on. */
  label: string;
  state: RestartStepState;
}

/**
 * The strip for a stage. Total, so every stage has a strip even where nothing
 * draws one (`ready`) — a partial function here would be a crash on a stage
 * added later, in a dialog that cannot be closed.
 *
 *   quitting/restarting/reconnecting — everything before it done, it live,
 *                                      everything after it upcoming;
 *   back                            — all three done;
 *   gave-up / ready                 — none done, none live. `gave-up` draws
 *                                     that greyed (the restart did not take,
 *                                     so no step may claim it did) and `ready`
 *                                     draws no strip at all.
 */
export function restartSteps(stage: RestartStage): RestartStep[] {
  const at = (RESTART_STEP_STAGES as readonly string[]).indexOf(stage);
  return RESTART_STEP_STAGES.map((step, i) => {
    let state: RestartStepState = "upcoming";
    if (stage === "back") state = "done";
    else if (at >= 0 && i < at) state = "done";
    else if (at >= 0 && i === at) state = "live";
    return { stage: step, label: state === "live" ? restartStageLabel(step) : restartStepWord(step), state };
  });
}

/** WHEN "about a minute" STOPS BEING TRUE. The body promises a number, and a
 *  promise that has visibly run out is worse than no promise at all — so at 90s
 *  (Akshil, 2026-09-19: the restart is nearer a minute than 15 seconds, so the
 *  promise, the flip and the cap all moved together)
 *  the sentence stops repeating it and says so. Comfortably past the honest
 *  case (a teardown plus a cold start of a signed bundle) and comfortably short
 *  of `RESTART_GIVE_UP_MS`, so the reader is told twice before the cap fires. */
export const RESTART_SLOW_MS = 90_000;

/** Whether the wait has outrun the sentence's own estimate. `requestedAt` is
 *  the press (shared verbatim across windows), so every window flips at the
 *  same instant rather than at its own. */
export function restartIsSlow(requestedAt: number | null, now: number): boolean {
  if (requestedAt === null) return false;
  return now - requestedAt >= RESTART_SLOW_MS;
}
