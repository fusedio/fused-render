import { describe, expect, test } from "bun:test";
import type { ExpertEngineRow, ExpertItem } from "./api";
import { basename, checkProgress, consultMeta, engineOptions, lessonTags, modelOptions, pendingLine, scoreText, scoresLine, seenLabel, sortConsults, sortLessons } from "./expert";

const item = (p: Partial<ExpertItem>): ExpertItem => ({ id: "m", claim: "c", kind: "lesson", trust: "outcome", count: 1, sessions: [], about: [],
  created: 0, updated: 0, proposed: false, promoted: false, rejected: false, ...p });

describe("lessons", () => {
  test("most seen first, then the most recently touched", () => {
    const out = sortLessons([item({ id: "a", count: 1, updated: 9 }), item({ id: "b", count: 4 }), item({ id: "c", count: 1, updated: 20 })]);
    expect(out.map((x) => x.id)).toEqual(["b", "c", "a"]);
  });
  test("does not reorder the caller's array", () => {
    const xs = [item({ id: "a", count: 1 }), item({ id: "b", count: 2 })];
    sortLessons(xs);
    expect(xs[0].id).toBe("a");
  });
  test("seen N×", () => {
    expect(seenLabel(3)).toBe("seen 3×");
    expect(seenLabel(0)).toBe("seen 1×");
  });
  test("tags: user trust, then promoted over proposed", () => {
    expect(lessonTags(item({ trust: "user" }))).toEqual(["user"]);
    expect(lessonTags(item({ trust: "outcome", proposed: true }))).toEqual(["proposed"]);
    expect(lessonTags(item({ trust: "user", proposed: true, promoted: true }))).toEqual(["user", "promoted"]);
    expect(lessonTags(item({ trust: "gap" }))).toEqual([]);
  });
  test("the pending line", () => {
    expect(pendingLine(0, false)).toBe("No observations waiting.");
    expect(pendingLine(1, false)).toBe("1 observation waiting to be consolidated.");
    expect(pendingLine(3, true)).toBe("3 observations waiting to be consolidated (due now).");
  });
});

describe("consults", () => {
  test("newest first", () => {
    const c = (id: string, ts: number) => ({ id, ts, question: "", context: "", answer: "", refs: [], used: [], unsure: false, ms: 0 });
    expect(sortConsults([c("a", 1), c("b", 3), c("c", 2)]).map((x) => x.id)).toEqual(["b", "c", "a"]);
  });
  test("basename", () => {
    expect(basename("/u/canvases/team/sales-map/")).toBe("sales-map");
    expect(basename("C:\\x\\y")).toBe("y");
    expect(basename("")).toBe("");
    expect(basename(undefined)).toBe("");
  });
  test("meta: when, target, backend as far as known", () => {
    const now = 1_000_000_000_000, ts = now / 1000 - 120;
    expect(consultMeta({ ts, caller: { target: "/c/team/sales-map", backend: "codex" } }, now)).toBe("2 min ago · sales-map · codex");
    expect(consultMeta({ ts, caller: null }, now)).toBe("2 min ago");
  });
});

describe("checks", () => {
  test("score and the scores line", () => {
    expect(scoreText({ score: 11, total: 14 })).toBe("11 / 14");
    expect(scoreText(null)).toBe("");
    expect(scoresLine({ score: 11, total: 14 }, { score: 12, total: 14 })).toBe("Checks: 11/14 → 12/14");
    expect(scoresLine(null, { score: 12, total: 14 })).toBe("");
  });
  test("progress only while running", () => {
    expect(checkProgress({ running: true, run_id: "r", done: 3, total: 14 })).toBe("Running checks · 3 of 14");
    expect(checkProgress({ running: true, run_id: "r", done: 0, total: 0 })).toBe("Running checks…");
    expect(checkProgress({ running: false, run_id: null, done: 0, total: 0 })).toBe("");
    expect(checkProgress(null)).toBe("");
  });
});

describe("engine and model menus", () => {
  const rows: ExpertEngineRow[] = [
    { id: "claude", label: "Claude", enabled: true, models: [{ id: "sonnet", label: "Sonnet" }, { id: "opus", label: "Opus" }], default_model: "sonnet" },
    { id: "codex", label: "Codex", enabled: false, models: [{ id: "gpt", label: "GPT" }], default_model: "gpt" },
    { id: "copilot", label: "Copilot", enabled: true, models: [], default_model: "" },
  ];
  test("Auto plus the enabled engines; the current one stays even when off", () => {
    expect(engineOptions(rows, "auto").map(([v]) => v)).toEqual(["auto", "claude", "copilot"]);
    expect(engineOptions(rows, "codex")).toContainEqual(["codex", "Codex (off)"]);
  });
  test("the engine's models after its default", () => {
    expect(modelOptions(rows, "claude", "")).toEqual([["", "Default (Sonnet)"], ["sonnet", "Sonnet"], ["opus", "Opus"]]);
    expect(modelOptions(rows, "auto", "")).toEqual([["", "Default"]]);
    expect(modelOptions(rows, "copilot", "gpt-5")).toEqual([["", "Default"], ["gpt-5", "gpt-5"]]);
  });
});
