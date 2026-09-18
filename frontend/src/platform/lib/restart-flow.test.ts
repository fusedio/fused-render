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
  restartStageLabel,
  RESTART_GIVE_UP_MS,
  RESTART_RECONNECTING_FAILS,
  RESTART_STAGES,
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

test("a version that never moved is still back once the server had gone away", () => {
  // The weak signal, and the page needs it: a probe body with no `version` in
  // it, or a press made before any healthy probe recorded one, would otherwise
  // sit on "Reconnecting…" through a restart that worked perfectly.
  const noVersion = run([[press(T0, null), T0], [fail(), T0 + 5_000], [ok(null), T0 + 12_000]]);
  expect(noVersion.stage).toBe("back");
  const sameVersion = run([[press(), T0], [fail(), T0 + 5_000], [ok(OLD), T0 + 12_000]]);
  expect(sameVersion.stage).toBe("back");
});

test("a version move alone is back, with no outage seen at all", () => {
  // A restart quicker than one 5 s poll: nothing ever failed, but the number
  // moved, and only a new process can move it.
  expect(run([[press(), T0], [ok(NEW), T0 + 5_000]]).stage).toBe("back");
});

test("back latches — a later failure does not reopen the wait", () => {
  const back = run([[press(), T0], [fail(), T0 + 5_000], [ok(NEW), T0 + 12_000]]);
  expect(reduceRestart(back, fail(), T0 + 17_000).stage).toBe("back");
  expect(reduceRestart(back, tick(), T0 + 90_000).stage).toBe("back");
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
