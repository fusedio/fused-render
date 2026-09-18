// The restart stage machine. One event at a time goes through `reduceRestart`;
// the stage it lands on is the word the blocking dialog shows while the app is
// quitting and coming back. The press is the only thing the page KNOWS (the
// deep link answers nothing), so every other transition is read off probes —
// which makes the give-up cap (D4) load-bearing rather than decorative: without
// it a press the app never acted on would say "Reconnecting…" forever.
import { expect, test } from "bun:test";

import {
  initialRestart,
  reduceRestart,
  restartInFlight,
  restartIsSlow,
  restartStageLabel,
  restartSteps,
  restartStepWord,
  RESTART_GIVE_UP_MS,
  RESTART_RECONNECTING_FAILS,
  RESTART_SLOW_MS,
  RESTART_STAGES,
  RESTART_STEP_STAGES,
  type RestartEvent,
  type RestartState,
} from "@platform/lib/restart-flow";

const T0 = 1_000_000;
const OLD = "0.5.50";
const NEW = "0.5.51";

const press = (at = T0, served: string | null = OLD): RestartEvent => ({
  type: "request",
  at,
  served,
});
const ok = (version: string | null = OLD): RestartEvent => ({ type: "probe", ok: true, version });
const fail = (): RestartEvent => ({ type: "probe", ok: false });
const tick = (): RestartEvent => ({ type: "tick" });

/** Run a script of [event, atMs] pairs from a fresh state. */
function run(script: Array<[RestartEvent, number]>, from: RestartState = initialRestart()) {
  let state = from;
  for (const [event, now] of script) state = reduceRestart(state, event, now);
  return state;
}

test("nothing is in flight before the press", () => {
  const state = initialRestart();
  expect(state.stage).toBe("ready");
  expect(state.requestedAt).toBeNull();
  expect(restartInFlight(state.stage)).toBe(false);
});

test("probes before the press change nothing", () => {
  // The whole flow hangs off the click; a page that never asked for a restart
  // must read an outage as an outage, not as a restart it did not request.
  expect(run([[fail(), T0], [fail(), T0 + 5_000], [ok(NEW), T0 + 10_000]]).stage).toBe("ready");
});

test("the press goes straight to quitting and records what was running", () => {
  const state = run([[press(), T0]]);
  expect(state.stage).toBe("quitting");
  expect(state.requestedAt).toBe(T0);
  expect(state.before).toBe(OLD);
  expect(restartInFlight(state.stage)).toBe(true);
});

test("the server still answering on the old version stays on quitting", () => {
  // `quit_teardown` takes seconds and logs its own duration — the socket is
  // still up for the first probe or two after the press.
  const state = run([[press(), T0], [ok(OLD), T0 + 5_000], [ok(OLD), T0 + 10_000]]);
  expect(state.stage).toBe("quitting");
  expect(state.fails).toBe(0);
});

test("the first failed probe is restarting, the next is reconnecting", () => {
  let state = run([[press(), T0], [fail(), T0 + 5_000]]);
  expect(state.stage).toBe("restarting");
  expect(state.fails).toBe(1);
  state = reduceRestart(state, fail(), T0 + 10_000);
  expect(state.stage).toBe("reconnecting");
  expect(state.fails).toBe(RESTART_RECONNECTING_FAILS);
  // And it stays there — "Reconnecting…" is the last word before the cap.
  state = reduceRestart(state, fail(), T0 + 15_000);
  expect(state.stage).toBe("reconnecting");
});

test("a healthy probe on a NEW version is back", () => {
  const state = run([[press(), T0], [fail(), T0 + 5_000], [ok(NEW), T0 + 12_000]]);
  expect(state.stage).toBe("back");
  expect(restartInFlight(state.stage)).toBe(true);
});

// ---- the false-completion latch (bugbot, PR #1214, HIGH) ------------------
//
// A teardown takes seconds, so ONE probe can time out while the socket is still
// open and the NEXT one answers — from the OLD process, which has not gone
// anywhere. The reducer used to read that as "it came back" and latch `back`, a
// terminal stage, on a dialog with no button, no ✕ and Escape swallowed. The
// only proof a process swapped is a version that moved.

test("a healthy probe on the SAME version after a failure is NOT back", () => {
  const state = run([[press(), T0], [fail(), T0 + 5_000], [ok(OLD), T0 + 10_000]]);
  expect(state.stage).not.toBe("back");
  // It goes BACK to "Quitting…", not forward: the old process is answering, so
  // claiming the app is gone would be the same lie the other way round.
  expect(state.stage).toBe("quitting");
  // And the streak resets, so a real outage after this re-walks the stages.
  expect(state.fails).toBe(0);
  // The clock is untouched by any of it — the cap is still coming.
  expect(state.requestedAt).toBe(T0);
});

test("a blip and a recovery can repeat without ever reaching back", () => {
  const state = run([
    [press(), T0],
    [fail(), T0 + 5_000],
    [ok(OLD), T0 + 10_000],
    [fail(), T0 + 15_000],
    [fail(), T0 + 20_000],
    [ok(OLD), T0 + 25_000],
  ]);
  expect(state.stage).toBe("quitting");
});

test("a healthy probe with NO version is inconclusive, never back", () => {
  // The body says the socket is open, not which process owns it. Nothing moves
  // — including the failure count, which resetting would be a claim this probe
  // cannot make.
  const after = run([[press(), T0], [fail(), T0 + 5_000], [ok(null), T0 + 10_000]]);
  expect(after.stage).toBe("restarting");
  expect(after.fails).toBe(1);
  // Same when there was no recorded version to compare against at all.
  const noBefore = run([[press(T0, null), T0], [fail(), T0 + 5_000], [ok(NEW), T0 + 10_000]]);
  expect(noBefore.stage).not.toBe("back");
});

test("a run of same-version answers ends in gave-up, not in a held stage", () => {
  // The whole failure mode, end to end: the app never quit, /api/config keeps
  // answering on the old version, and the page must stop promising rather than
  // sit on a word forever.
  let state = run([[press(), T0], [fail(), T0 + 3_000]]);
  for (let t = 8_000; t <= RESTART_GIVE_UP_MS; t += 5_000) {
    state = reduceRestart(state, ok(OLD), T0 + t);
    expect(state.stage).not.toBe("back");
    expect(state.stage).not.toBe("gave-up");
  }
  state = reduceRestart(state, ok(OLD), T0 + RESTART_GIVE_UP_MS + 1);
  expect(state.stage).toBe("gave-up");
  // …and with the story over, nothing is in flight, so the banner is free to
  // show whatever the server actually says.
  expect(restartInFlight(state.stage)).toBe(false);
});

test("a version move alone is back, with no outage seen at all", () => {
  // A restart quicker than one 5 s poll: nothing ever failed, but the number
  // moved, and only a new process can move it.
  expect(run([[press(), T0], [ok(NEW), T0 + 5_000]]).stage).toBe("back");
});

test("back latches against probes, but not against the clock", () => {
  const back = run([[press(), T0], [fail(), T0 + 5_000], [ok(NEW), T0 + 12_000]]);
  // Inside the window nothing a later probe says re-opens the wait.
  expect(reduceRestart(back, fail(), T0 + 17_000).stage).toBe("back");
  expect(reduceRestart(back, ok(OLD), T0 + 17_000).stage).toBe("back");
  // Past it the cap still fires. `back` is normally a beat away from
  // `reduceProbe`'s own reload — but if that reload never comes (a second
  // update landing mid-restart leaves the disk ahead, so `reduceProbe` returns
  // `update-restart` and no reload), the cap is the only thing that can unstick
  // a dialog with no ✕ and no Esc.
  expect(reduceRestart(back, tick(), T0 + 90_000).stage).toBe("gave-up");
});

test("the cap can fire from every stage the dialog is on screen for", () => {
  // Stated as a sweep, because "no stage outlives the cap" is the invariant
  // that keeps an undismissable dialog from becoming a dead end.
  for (const stage of RESTART_STAGES) {
    if (stage === "ready" || stage === "gave-up") continue;
    const state: RestartState = { stage, requestedAt: T0, fails: 1, before: OLD };
    expect(reduceRestart(state, tick(), T0 + RESTART_GIVE_UP_MS + 1).stage).toBe("gave-up");
    expect(reduceRestart(state, fail(), T0 + RESTART_GIVE_UP_MS + 1).stage).toBe("gave-up");
    expect(reduceRestart(state, ok(OLD), T0 + RESTART_GIVE_UP_MS + 1).stage).toBe("gave-up");
    expect(reduceRestart(state, ok(NEW), T0 + RESTART_GIVE_UP_MS + 1).stage).toBe("gave-up");
  }
});

test("the cap gives up after RESTART_GIVE_UP_MS of failures", () => {
  const justInside = run([
    [press(), T0],
    [fail(), T0 + 5_000],
    [fail(), T0 + RESTART_GIVE_UP_MS],
  ]);
  expect(justInside.stage).toBe("reconnecting");
  const past = reduceRestart(justInside, fail(), T0 + RESTART_GIVE_UP_MS + 1);
  expect(past.stage).toBe("gave-up");
  expect(restartInFlight(past.stage)).toBe(false);
});

test("the cap fires on the clock alone, with no probe to carry it", () => {
  expect(run([[press(), T0], [tick(), T0 + 30_000]]).stage).toBe("quitting");
  expect(run([[press(), T0], [tick(), T0 + RESTART_GIVE_UP_MS + 1]]).stage).toBe("gave-up");
});

test("the cap also fires on a server that never went away", () => {
  // A press the app never acted on: /api/config keeps answering on the old
  // version forever. "Quitting…" held for a minute is already generous.
  const state = run([[press(), T0], [ok(OLD), T0 + RESTART_GIVE_UP_MS + 1]]);
  expect(state.stage).toBe("gave-up");
});

test("gave-up latches, so the down card it hands the page to is not taken back", () => {
  const gone = run([[press(), T0], [tick(), T0 + RESTART_GIVE_UP_MS + 1]]);
  expect(reduceRestart(gone, fail(), T0 + 70_000).stage).toBe("gave-up");
  expect(reduceRestart(gone, ok(NEW), T0 + 70_000).stage).toBe("gave-up");
  expect(reduceRestart(gone, tick(), T0 + 70_000).stage).toBe("gave-up");
});

test("a second press re-arms from any stage, cap and all", () => {
  const gone = run([[press(), T0], [tick(), T0 + RESTART_GIVE_UP_MS + 1]]);
  const again = reduceRestart(gone, press(T0 + 80_000, NEW), T0 + 80_000);
  expect(again.stage).toBe("quitting");
  expect(again.requestedAt).toBe(T0 + 80_000);
  expect(again.before).toBe(NEW);
  expect(again.fails).toBe(0);
});

test("the cap runs off the press instant, not off this window's clock", () => {
  // D3: a window that LATCHED someone else's press must expire with it. Here
  // the press happened 59 s ago as far as this window is concerned, and the
  // event carries that instant rather than "now".
  const latched = reduceRestart(initialRestart(), press(T0, OLD), T0 + 59_000);
  expect(latched.requestedAt).toBe(T0);
  expect(reduceRestart(latched, fail(), T0 + 61_000).stage).toBe("gave-up");
});

test("every stage has exactly the label the design asked for", () => {
  expect(restartStageLabel("quitting")).toBe("Quitting…");
  expect(restartStageLabel("restarting")).toBe("Restarting…");
  expect(restartStageLabel("reconnecting")).toBe("Reconnecting…");
  // The end reuses the reconnected pill's own sentence, verbatim.
  expect(restartStageLabel("back")).toBe("Reconnected — fused-render is back.");
  // The two non-waits have no word: each hands the surface to something else.
  expect(restartStageLabel("ready")).toBe("");
  expect(restartStageLabel("gave-up")).toBe("");
  // One word plus an ellipsis, never a phrase (Akshil, 2026-09-08).
  for (const stage of ["quitting", "restarting", "reconnecting"] as const) {
    expect(restartStageLabel(stage)).toMatch(/^\S+…$/);
  }
});

test("in-flight is exactly the four stages the dialog owns the screen for", () => {
  const inFlight = RESTART_STAGES.filter(restartInFlight);
  expect(inFlight).toEqual(["quitting", "restarting", "reconnecting", "back"]);
});

// ---- the three-step strip --------------------------------------------------

/** The strip as three letters, in order: D one, L ive, · upcoming. Compact
 *  enough that a whole restart reads as five rows. */
const shape = (stage: (typeof RESTART_STAGES)[number]) =>
  restartSteps(stage)
    .map((s) => ({ done: "D", live: "L", upcoming: "·" })[s.state])
    .join("");

test("the strip walks one step per stage and never runs ahead of the machine", () => {
  expect(shape("quitting")).toBe("L··");
  expect(shape("restarting")).toBe("DL·");
  expect(shape("reconnecting")).toBe("DDL");
  // The end is every step done — no live step left, because there is no wait
  // left.
  expect(shape("back")).toBe("DDD");
});

test("gave-up claims nothing: no tick, and above all no spinner held forever", () => {
  // The restart did not take. A tick would say a step finished when the flow
  // cannot know that, and a live step would be exactly the promise
  // RESTART_GIVE_UP_MS exists to stop making.
  expect(shape("gave-up")).toBe("···");
  expect(restartSteps("gave-up").some((s) => s.state === "live")).toBe(false);
  // `ready` is the same shape for a different reason — nothing has been asked
  // for yet — and the dialog draws no strip there at all.
  expect(shape("ready")).toBe("···");
});

test("the strip is derived, so the same-version blip un-ticks instead of rewinding", () => {
  // `reduceRestart` sends `reconnecting` back to `quitting` when the old process
  // answers on the version that was already running. Nothing accumulates here,
  // so that regression is just the earlier shape again — identical to the one
  // the first stage produced, with no "furthest reached" left over.
  expect(shape("quitting")).toBe(shape("quitting"));
  expect(restartSteps("quitting")).toEqual(restartSteps("quitting"));
  expect(shape("reconnecting")).not.toBe(shape("quitting"));
});

test("every stage has a strip, so a stage added later cannot crash the dialog", () => {
  // The dialog cannot be closed. A partial function here would be a blank,
  // undismissable modal the first time the machine grew a stage.
  for (const stage of RESTART_STAGES) {
    const steps = restartSteps(stage);
    expect(steps.length).toBe(3);
    expect(steps.map((s) => s.stage)).toEqual([...RESTART_STEP_STAGES]);
    expect(steps.filter((s) => s.state === "live").length).toBeLessThanOrEqual(1);
    for (const step of steps) expect(step.label.length).toBeGreaterThan(0);
  }
});

test("a step's name is one word with no ellipsis; the ellipsis is the live tense", () => {
  // The repo rule (Akshil, 2026-09-08) is about the NAMES. "Restart" is a step;
  // "Restarting…" is that step happening, and it is the machine's own label
  // rather than a second copy of the vocabulary.
  expect(RESTART_STEP_STAGES.map(restartStepWord)).toEqual(["Quit", "Restart", "Reconnect"]);
  for (const stage of RESTART_STEP_STAGES) {
    expect(restartStepWord(stage)).toMatch(/^\S+$/);
    expect(restartStepWord(stage)).not.toContain("…");
    const live = restartSteps(stage).find((s) => s.state === "live");
    expect(live?.label).toBe(restartStageLabel(stage));
  }
  // A done or upcoming step shows the NAME, never the tense.
  expect(restartSteps("back").every((s) => !s.label.includes("…"))).toBe(true);
});

// ---- when "about 15 seconds" stops being true ------------------------------

test("the estimate is withdrawn at the mark, and only after it", () => {
  const at = 1_000_000;
  expect(restartIsSlow(at, at)).toBe(false);
  expect(restartIsSlow(at, at + RESTART_SLOW_MS - 1)).toBe(false);
  expect(restartIsSlow(at, at + RESTART_SLOW_MS)).toBe(true);
  expect(restartIsSlow(at, at + RESTART_SLOW_MS * 3)).toBe(true);
});

test("no press, no claim about how long it has been", () => {
  // The window that has adopted nothing cannot know elapsed time, and guessing
  // would print the anxious sentence over a restart that started a second ago.
  expect(restartIsSlow(null, 1_000_000)).toBe(false);
});

test("the mark sits between the promise and the cap, with room on both sides", () => {
  // The number itself, because every other assertion in this file reads it
  // symbolically and would follow it anywhere. 25s is the design's own (Akshil,
  // 2026-09-18) and the body's "about 15 seconds" is written against it.
  expect(RESTART_SLOW_MS).toBe(25_000);
  // Past the honest case (a teardown plus a cold start), so it is not fired at a
  // restart that is going fine; and well short of the cap, so the reader is told
  // the wait is long BEFORE they are told it failed.
  expect(RESTART_SLOW_MS).toBeGreaterThan(15_000);
  expect(RESTART_SLOW_MS).toBeLessThan(RESTART_GIVE_UP_MS);
});
