// The walkthrough's state machine, driven with a fake recorder and a fake
// clock: the two things a screenshot cannot show are the SETTLE (a second click
// inside the stop, a discard racing it) and the WORD ASSIGNMENT.
import { describe, expect, test } from "bun:test";

import {
  assignWords,
  createRecorder,
  recClock,
  recIdleName,
  recSeatName,
  REC_TICK_MS,
  type RecAnnotation,
  type Recorder,
  type RecorderDeps,
} from "./rec";
import type { Transcript, TranscriptWord } from "./transcribe";

// ── the harness ────────────────────────────────────────────────────────────

interface FakeCapture {
  path: string;
  bytes: number;
  seconds: number;
  /** Held open so a test can land a second click INSIDE the stop's await. */
  hold?: boolean;
  fail?: Error;
  /** The START request held open, so a test can act inside the mic prompt's own
   *  window (`state === "starting"`) the way a reader pressing Esc does. */
  holdStart?: boolean;
  /** How that held start ends, when it ends in a refusal rather than a mic. */
  startFail?: Error;
  /** The dismissed start's own `cancel()` held open, so a test can press the
   *  mic INSIDE the teardown (`state === "cancelling"`). */
  holdCancel?: boolean;
}

function makeWorld(over: Partial<FakeCapture> = {}, transcript?: Transcript | Error) {
  const plan: FakeCapture = { path: "/rec/a.m4a", bytes: 4096, seconds: 12, ...over };
  const notes: RecAnnotation[] = [];
  const log: string[] = [];
  let clock = 1000;
  let epoch = 1;
  let armed = false;
  let releaseStop: (() => void) | null = null;
  let releaseStart: (() => void) | null = null;
  let releaseCancel: (() => void) | null = null;
  const timers = new Set<{ fn: () => void; ms: number }>();
  const delivered: Array<{ intro: string; spoke: boolean }> = [];

  const handle = {
    id: "c1",
    mode: "audio",
    path: plan.path,
    jobId: "j1",
    maxSeconds: 1800,
    state: "recording",
    url: "",
    stop: () => {
      log.push("stop");
      if (plan.fail) return Promise.reject(plan.fail);
      const done = {
        id: "c1",
        mode: "audio",
        state: "stopped",
        path: plan.path,
        url: "",
        seconds: plan.seconds,
        bytes: plan.bytes,
        maxSeconds: 1800,
        jobId: "j1",
      };
      if (!plan.hold) return Promise.resolve(done);
      return new Promise<typeof done>((res) => {
        releaseStop = () => res(done);
      });
    },
    cancel: () => {
      log.push("cancel");
      const done = {
        id: "c1",
        mode: "audio",
        state: "cancelled",
        path: null,
        url: null,
        seconds: plan.seconds,
        maxSeconds: 1800,
        jobId: "j1",
      };
      if (!plan.holdCancel) return Promise.resolve(done);
      return new Promise<typeof done>((res) => {
        releaseCancel = () => res(done);
      });
    },
  };

  let seq = 0;
  const deps: RecorderDeps = {
    capture: () => {
      log.push("audio");
      if (plan.holdStart) {
        return new Promise<never>((res, rej) => {
          releaseStart = () =>
            plan.startFail ? rej(plan.startFail) : res(handle as never);
        });
      }
      return Promise.resolve(handle as never);
    },
    warm: () => log.push("warm"),
    transcribe: (path: string) => {
      log.push("transcribe:" + path);
      if (transcript instanceof Error) return Promise.reject(transcript);
      return Promise.resolve(
        transcript || { text: "", words: [], segments: [] },
      );
    },
    notes: {
      add: (n) => notes.push(n),
      get: (id) => notes.find((a) => a.id === id),
      assign: (texts) => {
        for (const { id, text } of texts) {
          const c = notes.find((a) => a.id === id);
          if (c) c.spoken = text;
        }
      },
      remove: (ids) => {
        for (const id of ids) {
          const i = notes.findIndex((a) => a.id === id);
          if (i >= 0) notes.splice(i, 1);
        }
      },
    },
    mode: {
      isArmed: () => armed,
      capable: () => true,
      // AN ARM BUMPS THE EPOCH, as the real machine's does (`mode.ts`): the
      // recorder now arms BEFORE the mic prompt and reads its own epoch back
      // from that arm, so a fake that kept the number still would hand every
      // start the epoch of the round before it.
      arm: () => {
        armed = true;
        epoch += 1;
        log.push("arm");
      },
      disarm: () => {
        armed = false;
        log.push("disarm");
      },
      epoch: () => epoch,
      syncParam: () => log.push("sync"),
    },
    deliver: (intro, spoke) => delivered.push({ intro, spoke }),
    newId: () => "id" + ++seq,
    now: () => clock,
    wallNow: () => 1700000000000,
    setInterval: (fn, ms) => {
      const t = { fn, ms };
      timers.add(t);
      return t;
    },
    clearInterval: (id) => timers.delete(id as { fn: () => void; ms: number }),
    warn: (m, d) => log.push("warn:" + m + " " + String(d)),
    alert: (m) => log.push("alert:" + m),
  };

  return {
    rec: createRecorder(deps),
    notes,
    log,
    delivered,
    timers,
    tick: (ms: number) => {
      clock += ms;
      for (const t of timers) t.fn();
    },
    advance: (ms: number) => {
      clock += ms;
    },
    releaseStop: () => releaseStop && releaseStop(),
    releaseStart: () => releaseStart && releaseStart(),
    releaseCancel: () => releaseCancel && releaseCancel(),
    setEpoch: (n: number) => {
      epoch = n;
    },
    isArmed: () => armed,
    /** The reader was in Comment mode BEFORE the mic — the one case where the
     *  start does not own the arming it finds. */
    setArmed: (v: boolean) => {
      armed = v;
    },
  };
}

const words = (pairs: Array<[number, string]>): TranscriptWord[] =>
  pairs.map(([start, text]) => ({ start, text }));

const transcriptOf = (pairs: Array<[number, string]>, text = ""): Transcript => ({
  text,
  words: words(pairs),
  segments: [{ text: text || pairs.map((p) => p[1]).join(" "), startSecond: pairs[0]?.[0] ?? 0 }],
});

async function record(world: ReturnType<typeof makeWorld>): Promise<Recorder> {
  await world.rec.begin();
  return world.rec;
}

// ── the clock ──────────────────────────────────────────────────────────────

describe("recClock (T:7790)", () => {
  test("m:ss, no leading zero on the minutes", () => {
    expect(recClock(0)).toBe("0:00");
    expect(recClock(7.9)).toBe("0:07");
    expect(recClock(59.99)).toBe("0:59");
    expect(recClock(60)).toBe("1:00");
    expect(recClock(61.4)).toBe("1:01");
    expect(recClock(605)).toBe("10:05");
    expect(recClock(3600)).toBe("60:00");
  });
});

describe("the label (T:7797-7803)", () => {
  test("the clock alone until the first click, then ' · N'", async () => {
    const w = makeWorld();
    await record(w);
    expect(w.rec.snapshot().status).toBe("0:00");
    w.tick(2400);
    expect(w.rec.snapshot().status).toBe("0:02");
    w.rec.mark({ kind: "element" });
    expect(w.rec.snapshot().status).toBe("0:02 · 1");
    w.rec.markPoint(10, 20, null);
    expect(w.rec.snapshot().status).toBe("0:02 · 2");
  });

  test("the tick is 250 ms, and it stops when the recording does", async () => {
    const w = makeWorld();
    await record(w);
    expect([...w.timers].map((t) => t.ms)).toEqual([REC_TICK_MS]);
    await w.rec.end();
    expect(w.timers.size).toBe(0);
  });
});

describe("annRecStamp (T:7944)", () => {
  test("seconds to a TENTH, never the raw float", async () => {
    const w = makeWorld();
    await record(w);
    w.advance(2415);
    expect(w.rec.stamp()).toBe(2.4);
    w.advance(50);
    expect(w.rec.stamp()).toBe(2.5);
  });

  test("a mark carries the stamp and the page coordinates", async () => {
    const w = makeWorld();
    await record(w);
    w.advance(3200);
    w.rec.markPoint(10, 20, { scrollX: 5, scrollY: 100 }, "#main > div:nth-of-type(2)");
    // A point mark's `x`/`y`/`nearPath` are the geometry module's fields, not
    // this module's type — read through the anchor's own shape.
    expect(w.notes[0] as unknown as Record<string, unknown>).toEqual({
      id: "id1",
      kind: "point",
      spoken: "",
      createdAt: 1700000000000,
      t: 3.2,
      x: 15,
      y: 120,
      nearPath: "#main > div:nth-of-type(2)",
    });
  });

  test("a mark outside a recording is refused, not stamped against a dead clock", () => {
    const w = makeWorld();
    expect(w.rec.mark({ kind: "element" })).toBeNull();
    expect(w.rec.markPoint(1, 2, null)).toBeNull();
    expect(w.notes).toHaveLength(0);
  });
});

// ── the matcher ────────────────────────────────────────────────────────────

describe("assignWords (T:8097)", () => {
  const marks = (ts: number[]): RecAnnotation[] =>
    ts.map((t, i) => ({ id: "m" + i, spoken: "", t }));

  test("every unit goes to exactly ONE click — nearest by start time", () => {
    const out = assignWords(
      marks([2, 6]),
      words([
        [2.1, "this"],
        [2.6, "button"],
        [5.8, "and"],
        [6.4, "this"],
      ]),
    );
    expect(out.texts).toEqual([
      { id: "m0", text: "this button" },
      { id: "m1", text: "and this" },
    ]);
    expect(out.intro).toBe("");
  });

  test("clicks CLOSER together than any lead window still do not double-count", () => {
    const out = assignWords(
      marks([3, 3.4]),
      words([
        [3.1, "one"],
        [3.35, "two"],
      ]),
    );
    expect(out.texts).toEqual([
      { id: "m0", text: "one" },
      { id: "m1", text: "two" },
    ]);
  });

  test("everything before the FIRST click is the intro, not click 1's words", () => {
    const out = assignWords(
      marks([5]),
      words([
        [0.2, "okay"],
        [1.0, "so"],
        [5.2, "change"],
        [5.6, "this"],
      ]),
    );
    expect(out.intro).toBe("okay so");
    expect(out.texts).toEqual([{ id: "m0", text: "change this" }]);
  });

  test("a click made MID-SENTENCE keeps only the words after it", () => {
    const out = assignWords(
      marks([2.5]),
      words([
        [1.0, "make"],
        [1.4, "this"],
        [2.9, "blue"],
        [3.3, "please"],
      ]),
    );
    // 1.4 is 1.1 s before the click and 2.9 is 0.4 s after: nearest, not
    // "the segment the click fell inside".
    expect(out.intro).toBe("make this");
    expect(out.texts).toEqual([{ id: "m0", text: "blue please" }]);
  });

  test("leading spaces on words collapse in the join (T:8098)", () => {
    const out = assignWords(marks([1]), words([[1.1, " one"], [1.5, " two"]]));
    expect(out.texts).toEqual([{ id: "m0", text: "one two" }]);
  });

  test("marks are matched in TIME order however they were listed", () => {
    const out = assignWords(
      [
        { id: "late", spoken: "", t: 9 },
        { id: "early", spoken: "", t: 1 },
      ],
      words([
        [0.5, "intro"],
        [1.2, "first"],
        [9.2, "second"],
      ]),
    );
    expect(out.intro).toBe("intro");
    expect(out.texts).toEqual([
      { id: "early", text: "first" },
      { id: "late", text: "second" },
    ]);
  });

  test("a tie goes to the FIRST click in time — one owner, deterministically", () => {
    const out = assignWords(marks([2, 4]), words([[3, "middle"]]));
    expect(out.texts).toEqual([
      { id: "m0", text: "middle" },
      { id: "m1", text: "" },
    ]);
  });

  test("no marks, or no words, assigns nothing", () => {
    expect(assignWords([], words([[1, "x"]]))).toEqual({ texts: [], intro: "" });
    expect(assignWords(marks([1]), [])).toEqual({ texts: [], intro: "" });
  });

  test("an UNSTAMPED note is not a mark (a typed note in the same round)", () => {
    const out = assignWords([{ id: "typed", spoken: "by hand" }], words([[1, "x"]]));
    expect(out).toEqual({ texts: [], intro: "" });
  });
});

// ── the settle ─────────────────────────────────────────────────────────────

describe("end() (T:8132)", () => {
  test("the happy path: stop → transcribe → words on the marks → auto-send → disarm", async () => {
    const w = makeWorld({}, transcriptOf([
      [0.5, "okay"],
      [2.2, "make"],
      [2.6, "this"],
      [2.9, "blue"],
    ]));
    await record(w);
    w.advance(2000);
    w.rec.mark({ kind: "element", tag: "BUTTON" });
    await w.rec.end();
    expect(w.notes[0].spoken).toBe("make this blue");
    expect(w.delivered).toEqual([{ intro: "okay", spoke: true }]);
    expect(w.rec.snapshot().state).toBe("off");
    expect(w.rec.snapshot().status).toBe("");
    expect(w.isArmed()).toBe(false);
    expect(w.log).toContain("transcribe:/rec/a.m4a");
  });

  test("NO clicks: the whole transcript is the prompt (T:8263-8271)", async () => {
    const w = makeWorld({}, transcriptOf([[1, "the"], [2, "whole"]], "the  whole   thing"));
    await record(w);
    await w.rec.end();
    expect(w.delivered).toEqual([{ intro: "the whole thing", spoke: false }]);
  });

  test("every mark DELETED is the no-click case — the walkthrough is not dropped", async () => {
    // The chip's ✕ (and any other `store.remove`) never calls back into the
    // recorder, so `ids` still names two marks the store no longer has.
    // Branching on THAT list asked the matcher to spread the words over an
    // empty set, which is a blank intro by contract: nothing delivered, and the
    // spoken walkthrough silently gone (Bugbot, PR #1074).
    const w = makeWorld({}, transcriptOf([[1.2, "make"], [3.4, "these"]], "make these blue"));
    await record(w);
    w.advance(1000);
    w.rec.mark({ kind: "element" });
    w.advance(2000);
    w.rec.mark({ kind: "element" });
    w.notes.length = 0; // both chips ✕'d while the mic was still live
    await w.rec.end();
    expect(w.delivered).toEqual([{ intro: "make these blue", spoke: false }]);
  });

  test("a PARTIAL deletion matches against the marks that are still there", async () => {
    const w = makeWorld({}, transcriptOf([
      [0.5, "okay"],
      [1.2, "first"],
      [3.2, "second"],
    ]));
    await record(w);
    w.advance(1000);
    w.rec.mark({ kind: "element" }); // id1, t = 1
    w.advance(2000);
    w.rec.mark({ kind: "element" }); // id2, t = 3
    w.notes.splice(0, 1); // the FIRST mark is ✕'d
    await w.rec.end();
    // The survivor takes what was said nearest IT, and the intro boundary is
    // re-read off the surviving first click instead of the deleted one.
    expect(w.notes).toHaveLength(1);
    expect(w.notes[0].id).toBe("id2");
    expect(w.notes[0].spoken).toBe("second");
    expect(w.delivered).toEqual([{ intro: "okay first", spoke: true }]);
  });

  test("a transcription that assigned NOTHING sends nothing (T:8272-8290)", async () => {
    const w = makeWorld({}, { text: "", words: [], segments: [] });
    await record(w);
    w.advance(1000);
    w.rec.mark({ kind: "element" });
    await w.rec.end();
    expect(w.delivered).toEqual([]);
    expect(w.notes[0].spoken).toBe("");
    expect(w.notes).toHaveLength(1); // stamped and empty, editable by hand
  });

  test("the statuses pass through Stopping… then Transcribing… (T:8181, 8243)", async () => {
    const w = makeWorld({ hold: true }, transcriptOf([[1, "hi"]]));
    await record(w);
    const settling = w.rec.end();
    expect(w.rec.snapshot().state).toBe("stopping");
    expect(w.rec.snapshot().status).toBe("Stopping…");
    expect(w.rec.snapshot().busy).toBe(true);
    w.releaseStop();
    await settling;
    expect(w.rec.snapshot().state).toBe("off");
  });

  test("a SECOND click during the stop is a no-op (T:8133)", async () => {
    const w = makeWorld({ hold: true }, transcriptOf([[1, "hi"]]));
    await record(w);
    const settling = w.rec.end();
    await w.rec.end();
    await w.rec.discard();
    w.releaseStop();
    await settling;
    expect(w.log.filter((l) => l === "stop")).toHaveLength(1);
    expect(w.log).not.toContain("cancel");
  });

  test("a failed STOP costs nobody the walkthrough (T:8182-8188)", async () => {
    const w = makeWorld({ fail: new Error("InvalidStateError") });
    await record(w);
    w.advance(1000);
    w.rec.mark({ kind: "element" });
    await w.rec.end();
    expect(w.log).toContain("warn:walkthrough stop failed: InvalidStateError");
    expect(w.log).not.toContain("transcribe:/rec/a.m4a");
    expect(w.notes).toHaveLength(1);
    expect(w.rec.snapshot().state).toBe("off");
    expect(w.isArmed()).toBe(false);
  });

  test("a transcription that GIVES UP still hands the seat back (T:8302-8318)", async () => {
    // The `jobId` the listing never carries — the watch is bounded and rejects
    // with the reporting sentence rather than sitting in "Transcribing…"
    // forever (Bugbot, PR #1074). The recovery is the `finally`'s: status
    // cleared, busy dropped, mode disarmed, so the Comment seat is live again.
    const w = makeWorld({}, new Error("the transcription job is no longer being reported"));
    await record(w);
    w.advance(1000);
    w.rec.mark({ kind: "element" });
    await w.rec.end();
    expect(w.log).toContain(
      "warn:spoken annotation transcription failed: the transcription job is no longer being reported",
    );
    expect(w.rec.snapshot().state).toBe("off");
    expect(w.rec.snapshot().status).toBe("");
    expect(w.rec.snapshot().busy).toBe(false);
    expect(w.isArmed()).toBe(false);
    // The marks survive the failure, stamped and editable by hand.
    expect(w.notes).toHaveLength(1);
    expect(w.delivered).toEqual([]);
  });

  test("a stop that landed on the start's own beat is nothing to transcribe (T:8199)", async () => {
    const w = makeWorld({ seconds: 0.3 });
    await record(w);
    await w.rec.end();
    expect(w.log).not.toContain("transcribe:/rec/a.m4a");
    expect(w.rec.snapshot().state).toBe("off");
  });

  test("an EMPTY file is the same branch (T:8199)", async () => {
    const w = makeWorld({ bytes: 0 });
    await record(w);
    await w.rec.end();
    expect(w.log).not.toContain("transcribe:/rec/a.m4a");
  });

  test("a failed TRANSCRIPTION leaves the marks stamped and empty (T:8293)", async () => {
    const w = makeWorld({}, new Error("the transcription failed"));
    await record(w);
    w.advance(1000);
    w.rec.mark({ kind: "element" });
    await w.rec.end();
    expect(w.log).toContain(
      "warn:spoken annotation transcription failed: the transcription failed",
    );
    expect(w.notes[0].spoken).toBe("");
    expect(w.delivered).toEqual([]);
    expect(w.rec.snapshot().state).toBe("off");
    expect(w.rec.snapshot().status).toBe("");
    expect(w.isArmed()).toBe(false);
  });

  test("the disarm is EPOCH-GUARDED: a re-arm inside the settle is not ours to close (T:8190)", async () => {
    const w = makeWorld({ hold: true }, transcriptOf([[1, "hi"]]));
    await record(w);
    const settling = w.rec.end();
    w.setEpoch(99); // the reader re-armed meanwhile
    w.releaseStop();
    await settling;
    expect(w.isArmed()).toBe(true);
    expect(w.log).not.toContain("disarm");
  });
});

describe("discard() (T:8337)", () => {
  test("cancels the recording and DELETES the marks", async () => {
    const w = makeWorld();
    await record(w);
    w.advance(1000);
    w.rec.mark({ kind: "element" });
    w.rec.markPoint(1, 2, null);
    expect(w.notes).toHaveLength(2);
    await w.rec.discard();
    expect(w.log).toContain("cancel");
    expect(w.log).not.toContain("stop");
    expect(w.notes).toHaveLength(0);
    expect(w.rec.snapshot().state).toBe("off");
    expect(w.isArmed()).toBe(false);
  });

  test("nothing is transcribed and nothing is sent", async () => {
    const w = makeWorld({}, transcriptOf([[1, "hi"]]));
    await record(w);
    await w.rec.discard();
    expect(w.log.some((l) => l.startsWith("transcribe:"))).toBe(false);
    expect(w.delivered).toEqual([]);
  });

  test("Discarding… is the status while it settles (T:8366)", async () => {
    const w = makeWorld();
    await record(w);
    const going = w.rec.discard();
    expect(w.rec.snapshot().status).toBe("Discarding…");
    expect(w.rec.snapshot().busy).toBe(true);
    await going;
  });

  test("EARLIER rounds' notes are not this recording's to throw", async () => {
    const w = makeWorld();
    w.notes.push({ id: "old", spoken: "typed earlier" });
    await record(w);
    w.rec.mark({ kind: "element" });
    await w.rec.discard();
    expect(w.notes.map((n) => n.id)).toEqual(["old"]);
  });

  test("a discard outside a recording is a no-op", async () => {
    const w = makeWorld();
    await w.rec.discard();
    expect(w.log).toEqual([]);
  });
});

describe("begin() (T:7858)", () => {
  test("warms the transcriber, arms the mode, starts the clock, syncs the URL", async () => {
    const w = makeWorld();
    await w.rec.begin();
    // ARMED FIRST, before the prompt: the start window is a mode like any
    // other (Bugbot, PR #1074).
    expect(w.log).toEqual(["arm", "warm", "audio", "sync"]);
    expect(w.rec.snapshot().state).toBe("recording");
    expect(w.isArmed()).toBe(true);
  });

  test("ONE start request at most (T:7759)", async () => {
    const w = makeWorld();
    await Promise.all([w.rec.begin(), w.rec.begin()]);
    expect(w.log.filter((l) => l === "audio")).toHaveLength(1);
  });

  test("blocked through a settle — no second walkthrough on the epoch the ender will disarm (T:7859)", async () => {
    const w = makeWorld({ hold: true }, transcriptOf([[1, "hi"]]));
    await record(w);
    const settling = w.rec.end();
    await w.rec.begin();
    expect(w.log.filter((l) => l === "audio")).toHaveLength(1);
    w.releaseStop();
    await settling;
  });

  test("a machine that cannot record says so with ITS OWN sentence (T:7893)", async () => {
    const shouted: string[] = [];
    const failing = createRecorder({
      capture: () =>
        Promise.reject(
          Object.assign(new Error("Microphone access is off in System Settings"), {
            type: "unavailable",
          }),
        ),
      warm: () => {},
      transcribe: () => Promise.reject(new Error("never")),
      notes: { add: () => {}, get: () => undefined, assign: () => {}, remove: () => {} },
      mode: {
        isArmed: () => false,
        capable: () => true,
        arm: () => {},
        disarm: () => {},
        epoch: () => 1,
      },
      deliver: () => {},
      alert: (m) => shouted.push(m),
    });
    await failing.begin();
    expect(shouted).toEqual(["Cannot record — Microphone access is off in System Settings"]);
    expect(failing.snapshot().state).toBe("off");
  });

  test("refused when there is nothing to annotate (annCapable, T:7861)", async () => {
    let asked = 0;
    const rec = createRecorder({
      capture: () => {
        asked += 1;
        return Promise.reject(new Error("never"));
      },
      warm: () => {},
      transcribe: () => Promise.reject(new Error("never")),
      notes: { add: () => {}, get: () => undefined, assign: () => {}, remove: () => {} },
      mode: {
        isArmed: () => false,
        capable: () => false,
        arm: () => {},
        disarm: () => {},
        epoch: () => 1,
      },
      deliver: () => {},
    });
    await rec.begin();
    expect(asked).toBe(0);
    expect(rec.snapshot().state).toBe("off");
  });
});

// ── the start window ───────────────────────────────────────────────────────

// The mic prompt's own width. The mode machine calls it a recording
// (`AnnRecorder.recording()` counts "starting"), so both exits reach the
// recorder while the request is still out — and the only thing either can do is
// make sure the mic that arrives behind the reader is put straight back down
// (Bugbot, PR #1074).
describe("the START window (T:7898-7960)", () => {
  test("a dismissal inside `starting` cancels the arriving capture", async () => {
    const w = makeWorld({ holdStart: true });
    const begun = w.rec.begin();
    expect(w.rec.snapshot().state).toBe("starting");
    // Esc, or the disarm the mode machine's `set(false)` makes: `end()` while
    // the request is out is the dismissal, not a stop.
    await w.rec.end();
    expect(w.rec.snapshot().state).toBe("starting"); // still nothing to stop
    w.releaseStart();
    await begun;
    // STOPPED AND DELETED, not stopped and kept: the only thing in that file is
    // the time the prompt was up.
    // The mode was taken by the START and handed back BEFORE the teardown, so
    // the strip never wears an armed face while the mic is put down.
    expect(w.log).toEqual(["arm", "warm", "audio", "disarm", "cancel"]);
    expect(w.rec.snapshot().state).toBe("off");
    expect(w.rec.snapshot().busy).toBe(false);
    expect(w.isArmed()).toBe(false);
    // No session was ever opened: no clock, no marks, and the URL never said 2.
    expect(w.timers.size).toBe(0);
    expect(w.log).not.toContain("sync");
  });

  test("the bar's trash inside `starting` is the same dismissal", async () => {
    const w = makeWorld({ holdStart: true });
    const begun = w.rec.begin();
    await w.rec.discard();
    w.releaseStart();
    await begun;
    expect(w.log).toEqual(["arm", "warm", "audio", "disarm", "cancel"]);
    expect(w.rec.snapshot().state).toBe("off");
  });

  test("a dismissed start that then REFUSES says nothing — nobody is waiting", async () => {
    const w = makeWorld({ holdStart: true, startFail: new Error("Permission denied") });
    const begun = w.rec.begin();
    await w.rec.end();
    w.releaseStart();
    await begun;
    expect(w.log.some((l) => l.startsWith("alert:"))).toBe(false);
    expect(w.rec.snapshot().state).toBe("off");
    expect(w.log).toContain("disarm"); // the mode still goes back
  });

  test("a round armed DURING the window is not this start's to close (the epoch rule)", async () => {
    const w = makeWorld({ holdStart: true });
    const begun = w.rec.begin();
    await w.rec.end();
    w.setEpoch(99); // the reader re-armed while the prompt was up
    w.releaseStart();
    await begun;
    expect(w.log).toEqual(["arm", "warm", "audio", "cancel"]);
    expect(w.rec.snapshot().state).toBe("off");
  });

  // ARMED AT THE PRESS, not at the mic's arrival. Everything the mode hangs off
  // `annOn` — Esc, the nav lock (← Chats, the recent rows), the narrow view's
  // disarm — was OFF for the whole width of the prompt, because the arm used to
  // wait for the capture to come back (Bugbot, PR #1074).
  test("the mode is armed BEFORE the prompt, and the arm is this start's own epoch", async () => {
    const w = makeWorld({ holdStart: true });
    const begun = w.rec.begin();
    expect(w.rec.snapshot().state).toBe("starting");
    expect(w.isArmed()).toBe(true);
    // Before the capture is even asked for: the order in the log is the order
    // the reader's rules come alive in.
    expect(w.log).toEqual(["arm", "warm", "audio"]);
    w.releaseStart();
    await begun;
    // And the live recording does not arm a SECOND time on top of its own.
    expect(w.log.filter((l) => l === "arm")).toHaveLength(1);
    expect(w.rec.snapshot().state).toBe("recording");
  });

  test("a reader ALREADY in Comment mode keeps their round when the mic refuses", async () => {
    // Comment mode first, then the mic: the arming the start finds is the
    // READER's, so the refusal below is not this start's to undo.
    const theirs = makeWorld({ holdStart: true, startFail: new Error("Permission denied") });
    theirs.setArmed(true);
    const refused = theirs.rec.begin();
    theirs.releaseStart();
    await refused;
    expect(theirs.log).not.toContain("arm");
    expect(theirs.log).not.toContain("disarm");
    expect(theirs.isArmed()).toBe(true);
    expect(theirs.log.some((l) => l.startsWith("alert:"))).toBe(true);

    // …while a refusal on a mode the START took hands it straight back, rather
    // than leaving the reader in a Comment round they never asked for.
    const ours = makeWorld({ holdStart: true, startFail: new Error("Permission denied") });
    const begun = ours.rec.begin();
    ours.releaseStart();
    await begun;
    expect(ours.log).toEqual(["arm", "warm", "audio", "disarm", "alert:Cannot record — Permission denied"]);
    expect(ours.isArmed()).toBe(false);
  });

  // A DISARM THE RECORDER NEVER HEARD ABOUT — a host `annSetMode`, a mode
  // cycled while the request was out. Nobody raised the dismissal, so the reply
  // arrives believing it owns the mode; the epoch it was started under says
  // otherwise, and the capture is torn down rather than armed on a round that
  // is not its own (Bugbot, PR #1074).
  test("a capture landing after a disarm it never heard about is torn down, never armed", async () => {
    const w = makeWorld({ holdStart: true });
    const begun = w.rec.begin();
    w.setEpoch(99); // a NEW arming owns the mode now
    w.releaseStart();
    await begun;
    expect(w.log).toEqual(["arm", "warm", "audio", "cancel"]);
    expect(w.rec.snapshot().state).toBe("off");
    // No clock, no session, no "2" — and the round that owns the mode now is
    // left exactly as it was.
    expect(w.timers.size).toBe(0);
    expect(w.log).not.toContain("sync");
    expect(w.log).not.toContain("disarm");
  });

  // THE TEARDOWN IS NOT `off`. The dismissed start painted `off` and only THEN
  // awaited its `cancel()`, and `begin()` refuses nothing but `off`: a second
  // press inside that window opened a second capture while the first was still
  // being deleted — two live mics, one known handle (Bugbot, PR #1074).
  test("no second walkthrough can begin while a dismissed start is torn down", async () => {
    const w = makeWorld({ holdStart: true, holdCancel: true });
    const begun = w.rec.begin();
    await w.rec.end(); // the dismissal
    w.releaseStart();
    await Promise.resolve();
    await Promise.resolve();
    // The teardown is in flight, and it says so.
    expect(w.rec.snapshot().state).toBe("cancelling");
    // The mic is not a status seat here: nothing to say, nothing to hold.
    expect(w.rec.snapshot().status).toBe("");
    expect(w.rec.snapshot().busy).toBe(false);

    await w.rec.begin(); // the second press
    expect(w.log.filter((l) => l === "audio")).toHaveLength(1);

    w.releaseCancel();
    await begun;
    expect(w.rec.snapshot().state).toBe("off");
    // …and the seat works again once the mic is actually down.
    const again = w.rec.begin();
    await Promise.resolve();
    expect(w.log.filter((l) => l === "audio")).toHaveLength(2);
    w.releaseStart();
    await again;
    expect(w.rec.snapshot().state).toBe("recording");
  });

  test("a click inside the window mints nothing — there is no clock to stamp it against", async () => {
    const w = makeWorld({ holdStart: true });
    const begun = w.rec.begin();
    expect(w.rec.mark({ kind: "element" })).toBeNull();
    expect(w.rec.markPoint(3, 4, null)).toBeNull();
    expect(w.notes).toHaveLength(0);
    w.releaseStart();
    await begun;
    expect(w.rec.snapshot().marks).toBe(0);
  });
});

describe("the seat's names", () => {
  test("the settle names the seat for its status; rest gives the name back (T:8422)", () => {
    expect(recSeatName("off")).toEqual(recIdleName());
    expect(recSeatName("recording").label).toBe("Stop the recording");
    expect(recSeatName("recording").title).toBe("Recording — click to stop · Esc also stops it");
    expect(recSeatName("stopping").title).toBe("Stopping the recording…");
    expect(recSeatName("transcribing").title).toBe(
      "Transcribing the walkthrough — the notes send themselves when the words land",
    );
    expect(recSeatName("discarding").label).toBe("Discarding the recording");
  });
});

describe("subscribe", () => {
  test("every tick and every mark notifies, with a fresh snapshot identity", async () => {
    const w = makeWorld();
    let hits = 0;
    const off = w.rec.subscribe(() => {
      hits += 1;
    });
    await record(w);
    const first = w.rec.snapshot();
    w.tick(250);
    expect(w.rec.snapshot()).not.toBe(first);
    const before = hits;
    w.rec.mark({ kind: "element" });
    expect(hits).toBe(before + 1);
    off();
    w.tick(250);
    expect(hits).toBe(before + 1);
  });
});

// ── the teardown's ending (T:8796-8812) ─────────────────────────────────────

describe("abandon", () => {
  test("stops the microphone and NEVER transcribes, delivers or disarms", async () => {
    // T's `pagehide` handler is a bare `handle.stop().catch(() => {})` and says
    // why it goes no further: "this document is going away and there is no
    // panel to show one in". Reaching for `end()` here was not just a wasted
    // request — the same teardown runs on a React unmount, so an in-app
    // navigation fired a transcription AND `deliver`'s automatic send into a
    // conversation that no longer existed.
    const w = makeWorld(undefined, {
      text: "make this bigger",
      words: [{ text: "make", start: 1, end: 1.2 }],
      segments: [{ text: "make this bigger", startSecond: 1, endSecond: 2 }],
    });
    await record(w);
    w.tick(3000);
    w.rec.mark({ kind: "element" });
    expect(w.notes).toHaveLength(1);

    w.rec.abandon();

    // The mic is off, and the file was KEPT (CP-4): `stop`, never `cancel`.
    expect(w.log).toContain("stop");
    expect(w.log).not.toContain("cancel");
    // Nothing was transcribed, nothing was sent, and the marks are still there
    // — stamped and empty, editable by hand like any other pending note.
    expect(w.log.some((l) => l.startsWith("transcribe:"))).toBe(false);
    expect(w.delivered).toHaveLength(0);
    expect(w.notes).toHaveLength(1);
    // And it did not disarm: `annOn = false` is the teardown's OWN first line
    // (T:8797), not the recorder's to do on the way past.
    expect(w.log).not.toContain("disarm");
  });

  test("straight to `off`, with the clock stopped — there is no settle here", async () => {
    const w = makeWorld();
    await record(w);
    w.tick(2000);
    expect(w.rec.snapshot().state).toBe("recording");

    w.rec.abandon();

    // No "Stopping…"/"Transcribing…": nothing is being transcribed and nobody
    // is waiting for words, so a status painted onto a document mid-unload
    // would be a promise this ending does not make.
    expect(w.rec.snapshot().state).toBe("off");
    expect(w.rec.snapshot().status).toBe("");
    expect(w.rec.snapshot().busy).toBe(false);
    expect(w.rec.snapshot().marks).toBe(0);
    // The tick is cancelled, not left running into a dead label.
    expect(w.timers.size).toBe(0);
  });

  test("through the START WINDOW it is the dismissal, like every other exit", async () => {
    const w = makeWorld({ holdStart: true });
    const begun = w.rec.begin();
    expect(w.rec.snapshot().state).toBe("starting");

    w.rec.abandon();
    w.releaseStart();
    await begun;

    // `begin()` cancels its own reply, so the mic never comes up behind a
    // document that is already going.
    expect(w.log).toContain("cancel");
    expect(w.rec.snapshot().state).toBe("off");
    expect(w.delivered).toHaveLength(0);
  });

  test("nothing recording: a no-op, and it does not throw", () => {
    const w = makeWorld();
    expect(() => w.rec.abandon()).not.toThrow();
    expect(w.log).not.toContain("stop");
    expect(w.rec.snapshot().state).toBe("off");
  });

  test("a failed stop is swallowed — an unloading document has nowhere to say it", async () => {
    const w = makeWorld({ fail: new Error("InvalidStateError") });
    await record(w);
    expect(() => w.rec.abandon()).not.toThrow();
    expect(w.rec.snapshot().state).toBe("off");
  });
});
