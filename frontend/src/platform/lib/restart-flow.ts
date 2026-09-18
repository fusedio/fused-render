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
//   back         — a probe answered again, on a different version than the one
//                  that was running when the button was pressed.
//   gave-up      — RESTART_GIVE_UP_MS passed without it coming back. The
//                  restart story ends here and the ordinary "down" card takes
//                  over: a stage label held forever is a promise the page
//                  cannot keep.
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

/** The cap (D4). One minute is long enough for a teardown plus a cold start of
 *  a signed bundle, and short enough that a restart that is never coming back
 *  hands the page to the "down" card while the reader is still watching. */
export const RESTART_GIVE_UP_MS = 60_000;

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

  // Nothing in flight, or a story that has already ended: both terminal stages
  // LATCH. `gave-up` in particular must not be undone by a late probe, or the
  // modal would reappear over the down card the cap just handed the page to.
  if (state.requestedAt === null) return state;
  if (state.stage === "ready" || state.stage === "back" || state.stage === "gave-up") return state;

  const expired = now - state.requestedAt > RESTART_GIVE_UP_MS;

  if (event.type === "probe" && event.ok) {
    // TWO WAYS TO KNOW IT CAME BACK, and the page needs both. The version
    // moving is the strong one (the process is provably not the one that was
    // running). The weak one — any answer at all after the server had stopped
    // answering — is what covers a probe body with no version in it and a
    // press made before any healthy probe had recorded one; without it a
    // restart that worked perfectly would sit on "Reconnecting…" until the cap.
    const versionMoved = !!event.version && !!state.before && event.version !== state.before;
    const wasGone = state.stage === "restarting" || state.stage === "reconnecting";
    if (versionMoved || wasGone) return { ...state, stage: "back", fails: 0 };
    // Still the same process answering: the teardown has not reached the
    // socket yet. Stay on "Quitting…" — and still honour the cap, because a
    // press the app never acted on looks exactly like this forever.
    if (expired) return { ...state, stage: "gave-up", fails: 0 };
    return { ...state, fails: 0 };
  }

  if (event.type === "probe") {
    const fails = state.fails + 1;
    if (expired) return { ...state, stage: "gave-up", fails };
    const stage: RestartStage = fails >= RESTART_RECONNECTING_FAILS ? "reconnecting" : "restarting";
    return { ...state, stage, fails };
  }

  return expired ? { ...state, stage: "gave-up" } : state;
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
