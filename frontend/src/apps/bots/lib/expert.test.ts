import { describe, expect, test } from "bun:test";
import type { ExpertEngineRow, ExpertItem, ExpertProposal } from "./api";
import { anyPreparing, basename, checkProgress, consultMeta, engineNote, engineOptions, lessonTags, modelOptions, openProposals, pendingLine, proposalLine, proposalText, scoreText,
  scoresLine, seenLabel, sortConsults, sortLessons, splitLessons } from "./expert";

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
    expect(lessonTags(item({ proposed: true, rejected: true }))).toEqual(["not proposed again"]);
    expect(lessonTags(item({ proposed: true, rejected: true, promoted: true }))).toEqual(["promoted"]);
    expect(lessonTags(item({ rejected: true }))).toEqual(["not proposed again"]);
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
    { id: "codex", label: "Codex", enabled: false, models: [{ id: "gpt", label: "GPT" }], default_model: "gpt", note: "Codex keeps its own shell." },
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
  test("the selected engine's note, or nothing", () => {
    expect(engineNote(rows, "codex")).toBe("Codex keeps its own shell.");
    expect(engineNote(rows, "claude")).toBe("");
    expect(engineNote(rows, "auto")).toBe("");
  });
});

describe("memory split and proposals", () => {
  const it = (id: string, count: number, indexed?: boolean): ExpertItem => ({ id, claim: id, kind: "lesson", trust: "outcome", count, sessions: [], about: [],
    created: 0, updated: 0, proposed: false, promoted: false, rejected: false, ...(indexed === undefined ? {} : { indexed }) });
  test("seen-once lessons apart; no flag reads as indexed", () => {
    const { indexed, seenOnce } = splitLessons([it("a", 1, false), it("b", 3), it("c", 2, true), it("d", 1, false)]);
    expect(indexed.map((x) => x.id)).toEqual(["b", "c"]);
    expect(seenOnce.map((x) => x.id)).toEqual(["a", "d"]);
  });
  const p = (id: string, status: ExpertProposal["status"], ts: number, error?: string): ExpertProposal => ({ id, ts, status, action: "create", skill: null, title: "",
    trigger: "", body: "", diff: "", items: [], score_before: null, score_after: null, card_seq: null, ...(error ? { error } : {}) });
  test("the open ones newest first, each in its sentence", () => {
    const open = openProposals([p("a", "approved", 5), p("b", "preparing", 1), p("c", "shown", 3), p("d", "failed", 2, "checks dropped"), p("e", "rejected", 9)]);
    expect(open.map((x) => x.id)).toEqual(["c", "d", "b"]);
    expect(proposalLine(p("b", "preparing", 1))).toBe("Drafting a skill proposal and running the checks…");
    expect(proposalLine(p("r", "ready", 1))).toBe("A skill proposal is waiting for your answer in the chat.");
    expect(proposalLine(p("c", "shown", 1))).toBe("A skill proposal is waiting for your answer in the chat.");
    expect(proposalLine(p("d", "failed", 1, "checks dropped"))).toBe("A skill proposal failed: checks dropped");
    expect(proposalLine(p("a", "approved", 1))).toBe("");
    expect(openProposals(null)).toEqual([]);
  });
  test("polls only while one is preparing", () => {
    expect(anyPreparing([p("a", "shown", 1)])).toBe(false);
    expect(anyPreparing([p("a", "shown", 1), p("b", "preparing", 2)])).toBe(true);
    expect(anyPreparing(null)).toBe(false);
  });
});

describe("proposalText", () => {
  test("drops the Checks line and a trailing Approve?", () => {
    const t = "Update skill foo: handle bar.\n\nChecks: 13/14 now → 13/14 with this change.\n\nApprove?";
    expect(proposalText(t)).toBe("Update skill foo: handle bar.");
    expect(proposalText("A\nChecks: 1/2\nB")).toBe("A\nB");
    expect(proposalText("Approve? is not last\nB")).toBe("Approve? is not last\nB");
    expect(proposalText(null)).toBe("");
  });
});
