// THE SIDEBAR'S FAST LANE — why "1 running" appears the moment a run starts
// rather than up to thirty seconds later (Akshil, 2026-09-12: "should be
// instant… everywhere in UI").
//
// The loop is pure by construction: it takes its fetch, its clock, its
// visibility and its stop switch, so this drives all four by hand instead of
// holding a real connection open for 25 seconds. What the STORE does with it —
// start with the first reader, stand down for a feeder — is read out of the
// source beside it, the way the rest of this suite reads wiring a DOM-less test
// cannot otherwise hold.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { TaskChanges } from "@platform/lib/api";
import { FAST_LANE_BACKOFF_MS, watchTaskChanges } from "./tasksPulse";

const HERE = new URL(".", import.meta.url).pathname;
const STORE = readFileSync(join(HERE, "tasksPulse.ts"), "utf8");

interface Drive {
  /** Every `since` the loop asked with, in order. */
  asked: number[];
  /** How many times it said "read the pulse now". */
  pokes: number;
  /** Every backoff it slept. */
  slept: number[];
  done: Promise<void>;
  stop(): void;
  /** Let the tab become visible again. */
  show(): void;
}

/** Run the loop over a scripted list of answers; it stops when they run out. */
function drive(
  answers: Array<TaskChanges | Error>,
  opts: { visible?: boolean } = {},
): Drive {
  const asked: number[] = [];
  const slept: number[] = [];
  let pokes = 0;
  let stopped = false;
  let visible = opts.visible !== false;
  let wake: (() => void) | null = null;
  const out: Drive = {
    asked,
    slept,
    get pokes() {
      return pokes;
    },
    done: Promise.resolve(),
    stop() {
      stopped = true;
      wake?.();
    },
    show() {
      visible = true;
      wake?.();
      wake = null;
    },
  };
  out.done = watchTaskChanges({
    changes: async (since) => {
      asked.push(since);
      const next = answers.shift();
      // The script is the test's own clock: when it runs out the loop is done.
      if (next === undefined) {
        stopped = true;
        return { generation: since < 0 ? 0 : since, rows: [], gone: [] };
      }
      if (next instanceof Error) throw next;
      return next;
    },
    onChange: () => {
      pokes += 1;
    },
    visible: () => visible,
    untilVisible: () =>
      new Promise<void>((resolve) => {
        wake = resolve;
      }),
    sleep: async (ms) => {
      slept.push(ms);
    },
    stopped: () => stopped,
  });
  return out;
}

describe("the pulse's fast lane", () => {
  it("opens with a handshake, and does not call that news", async () => {
    // `since < 0` is answered at once with the current generation and no keys
    // (tasks_watch.wait). A poke here would make every mount pay for two reads
    // of the pulse: `useTasksPulse` already reads it on mount.
    const d = drive([{ generation: 7, rows: [], gone: [] }]);
    await d.done;
    expect(d.asked[0]).toBe(-1);
    expect(d.pokes).toBe(0);
    // …and the next question carries the generation the handshake named.
    expect(d.asked[1]).toBe(7);
  });

  it("reads the pulse the moment the generation moves", async () => {
    const d = drive([
      { generation: 7, rows: [], gone: [] },
      // A run started: the watcher answers within milliseconds of the event.
      { generation: 8, rows: [{ key: "s1" } as never], gone: [] },
      // …and a lapse with nothing new is not news, whatever it costs to ask.
      { generation: 8, rows: [], gone: [] },
      { generation: 9, rows: [], gone: ["s2"] },
    ]);
    await d.done;
    expect(d.pokes).toBe(2);
    expect(d.asked.slice(0, 4)).toEqual([-1, 7, 8, 8]);
  });

  it("reloads on `full` — a server that restarted and counts from zero", async () => {
    const d = drive([
      { generation: 7, rows: [], gone: [] },
      { generation: 2, full: true },
      { generation: 3, rows: [], gone: [] },
    ]);
    await d.done;
    // `full` is news by definition (one poke), and the loop carries on from the
    // generation it named rather than from the one it can no longer reach — so
    // the very next move of that new counter is news too.
    expect(d.pokes).toBe(2);
    expect(d.asked[2]).toBe(2);
  });

  it("backs off instead of hammering a server that is restarting", async () => {
    const d = drive([
      { generation: 7, rows: [], gone: [] },
      new Error("connection refused"),
      { generation: 8, rows: [{ key: "s1" } as never] },
    ]);
    await d.done;
    expect(d.slept).toEqual([FAST_LANE_BACKOFF_MS]);
    // A failed call is not a generation: the retry asks the same question.
    expect(d.asked[1]).toBe(7);
    expect(d.asked[2]).toBe(7);
    expect(d.pokes).toBe(1);
  });

  it("parks on a hidden tab and picks up when it comes back", async () => {
    const d = drive([{ generation: 7, rows: [], gone: [] }], { visible: false });
    // Nothing at all is asked while the window is in the background: the
    // browser throttles these timers anyway, and a connection held open per
    // hidden window is the one cost this must not add.
    await Promise.resolve();
    expect(d.asked.length).toBe(0);
    d.show();
    await d.done;
    expect(d.asked[0]).toBe(-1);
  });

  it("ends when it is stood down, even parked on a hidden tab", async () => {
    // The sidebar unmounting while the window is in the background left a
    // listener and a promise nothing could settle, once. The stop switch has to
    // reach a wait that has no timer and no request behind it.
    const d = drive([{ generation: 7 }], { visible: false });
    d.stop();
    await d.done;
    expect(d.asked.length).toBe(0);
  });
});

describe("how the lane coexists with the Tasks page", () => {
  it("runs only while this module is the poller", () => {
    // ONE POLLER. The Tasks page runs this very long-poll itself and publishes
    // what it learns (`publishTasks`), which is how the sidebar comes along
    // without a second connection — so a feeder stands the lane down exactly as
    // it stands the interval down, and `schedule()` is the one place that
    // decides both.
    expect(STORE).toContain("syncFastLane();");
    expect(STORE).toMatch(
      /const wanted =\s*\n?\s*listeners\.size \+ rowListeners\.size > 0 &&\s*\n?\s*feeders === 0/,
    );
    expect(STORE).toContain("lane?.stop();");
    // The interval is untouched underneath: the lane makes the news EARLY, it
    // is not the only thing that brings it.
    expect(STORE).toContain("const ACTIVE_MS = 10_000;");
    expect(STORE).toContain("const IDLE_MS = 30_000;");
    expect(STORE).toContain("timer = window.setTimeout(poll, pulse.running > 0 ? ACTIVE_MS : IDLE_MS);");
  });

  it("asks the same endpoint the page's own lane asks, and aborts on the way out", () => {
    expect(STORE).toContain("getTaskChanges(since, CHANGES_WAIT_S, inflight.signal)");
    expect(STORE).toContain("inflight?.abort();");
    // The doorbell answers with a small `/api/tasks/pulse`, not with the page's
    // full rows: this module has never wanted titles or paths.
    expect(STORE).toContain("void poll();");
    expect(STORE).not.toContain("getTasks()");
  });
});
