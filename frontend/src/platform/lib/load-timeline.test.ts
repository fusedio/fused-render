import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import {
  beginLoad,
  finishLoad,
  formatTimeline,
  lastRequestMs,
  loadTimelines,
  markLoad,
  openTimeline,
  resetLoadTimelines,
  type LoadTimeline,
} from "./load-timeline";
import { DEBUG_KEY } from "./debug-flag";

const g = globalThis as { localStorage?: unknown };
const hadStorage = g.localStorage;
const realInfo = console.info;
let printed: string[] = [];

beforeEach(() => {
  resetLoadTimelines();
  printed = [];
  console.info = (line: string) => void printed.push(line);
});
afterEach(() => {
  console.info = realInfo;
  g.localStorage = hadStorage;
});

describe("a load timeline", () => {
  it("records each stage once, in order, and ends on finish", () => {
    beginLoad("/c/udf_1.py", "udf_1.py", { stat: 12 });
    markLoad("/c/udf_1.py", "gate");
    markLoad("/c/udf_1.py", "mount");
    markLoad("/c/udf_1.py", "gate"); // a second mark of a stage is ignored
    finishLoad("/c/udf_1.py", "ready");
    const [t] = loadTimelines();
    expect(t.stages.map((s) => s.stage)).toEqual(["gate", "mount", "ready"]);
    expect(t.end).toBe("ready");
    expect(t.total).not.toBeNull();
    expect(t.before).toEqual({ stat: 12 });
    // Stage times never go backwards.
    const ats = t.stages.map((s) => s.at);
    expect([...ats].sort((a, b) => a - b)).toEqual(ats);
    expect(openTimeline("/c/udf_1.py")).toBeNull();
  });

  it("a finished timeline takes no more marks, and a second finish is a no-op", () => {
    beginLoad("k", "k");
    finishLoad("k", "ready");
    markLoad("k", "late");
    finishLoad("k", "again");
    const [t] = loadTimelines();
    expect(t.stages.map((s) => s.stage)).toEqual(["ready"]);
    expect(t.end).toBe("ready");
  });

  it("a view mounted without a begin starts its own timeline", () => {
    markLoad("/p/a.md", "mount", "a.md");
    expect(openTimeline("/p/a.md")?.label).toBe("a.md");
  });

  it("a reopen begins afresh and keeps the earlier run", () => {
    beginLoad("k", "k");
    markLoad("k", "gate");
    beginLoad("k", "k"); // reopened before it finished
    finishLoad("k", "ready");
    const all = loadTimelines();
    expect(all).toHaveLength(2);
    expect(all[0].end).toBeNull();
    expect(all[1].stages.map((s) => s.stage)).toEqual(["ready"]);
  });

  it("prints its summary only with the debug flag on", () => {
    beginLoad("a", "a");
    finishLoad("a", "ready");
    expect(printed).toEqual([]);
    g.localStorage = { getItem: (k: string) => (k === DEBUG_KEY ? "1" : null) };
    beginLoad("b", "b.py");
    finishLoad("b", "ready");
    expect(printed).toHaveLength(1);
    expect(printed[0]).toStartWith("[load] b.py: ready in ");
  });
});

it("keeps a bounded number of finished records", () => {
  for (let i = 0; i < 50; i++) {
    beginLoad(`f${i}`, `f${i}`);
    finishLoad(`f${i}`, "ready");
  }
  expect(loadTimelines()).toHaveLength(20);
  // The newest are kept and still refuse late marks; the oldest are gone.
  markLoad("f49", "late");
  expect(openTimeline("f49")).toBeNull();
  const all = loadTimelines();
  expect(all[all.length - 1].key).toBe("f49");
});

describe("formatTimeline", () => {
  const t = (over: Partial<LoadTimeline>): LoadTimeline => ({
    key: "k",
    label: "udf_1.py",
    t0: 200,
    stages: [
      { stage: "gate", at: 60 },
      { stage: "mount", at: 190 },
      { stage: "ready", at: 1250 },
    ],
    end: "ready",
    total: 1250,
    before: { stat: 21 },
    ...over,
  });

  it("gives each stage's time since the one before it", () => {
    expect(formatTimeline(t({}))).toBe(
      "[load] udf_1.py: ready in 1250 ms (stat 21 ms, page +200 ms) — gate +60 ms · mount +130 ms · ready +1060 ms",
    );
  });

  it("says when it is still loading", () => {
    expect(formatTimeline(t({ end: null, total: null, before: {}, stages: [{ stage: "gate", at: 60 }] }))).toBe(
      "[load] udf_1.py: still loading after 60 ms (page +200 ms) — gate +60 ms",
    );
  });
});

it("lastRequestMs is null when nothing matches, and never throws", () => {
  expect(lastRequestMs("/api/fs/stat?path=%2Fnowhere")).toBeNull();
});
