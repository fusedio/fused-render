import { describe, expect, test } from "bun:test";
import type { ExpertEngineRow, ExpertGap, ExpertItem, ExpertObs, ExpertProposal } from "./api";
import { anyPreparing, askedLabel, basename, consolidatingLine, gapStatusLabel, observationGaps, observationMeta, observationResultLabel, observationTypeLabel,
  researchingGap, sortObservations, unresolvedGapCount, checkProgress, consultMeta, engineNote, engineOptions, lessonTags, modelOptions, openProposals, proposalLine, proposalText, scoreText,
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
    expect(lessonTags(item({ trust: "research", indexed: false, proposed: true }))).toEqual(["found by research", "waiting for your approval", "proposed"]);
    expect(lessonTags(item({ trust: "research" }))).toEqual(["found by research"]);
    expect(lessonTags(item({ trust: "outcome", indexed: false }))).toEqual(["seen once"]);
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

const gap = (p: Partial<ExpertGap>): ExpertGap => ({ id: "g", question: "q", questions: [], count: 1, consults: [], sessions: [], created: 0, updated: 0,
  status: "open", attempts: 0, finding: null, proposal: null, card_seq: null, resolution: "", error: "", ...p });

describe("gaps", () => {
  test("status wording", () => {
    expect(gapStatusLabel(gap({ status: "open" }))).toBe("waiting");
    expect(gapStatusLabel(gap({ status: "researching" }))).toBe("researching…");
    expect(gapStatusLabel(gap({ status: "proposed" }))).toBe("skill fix proposed in chat");
    expect(gapStatusLabel(gap({ status: "asked" }))).toBe("asked you in chat");
    expect(gapStatusLabel(gap({ status: "answered", resolution: "skill" }))).toBe("answered by a new skill");
    expect(gapStatusLabel(gap({ status: "answered", resolution: "user" }))).toBe("answered by you");
    expect(gapStatusLabel(gap({ status: "dismissed", resolution: "dismissed" }))).toBe("dropped");
    expect(gapStatusLabel(gap({ id: "x", status: "open" }), "x")).toBe("researching…");
    expect(gapStatusLabel(gap({ id: "x", status: "open" }), "y")).toBe("waiting");
    expect(gapStatusLabel(gap({ id: "x", status: "dismissed" }), "x")).toBe("dropped");
  });
  test("unresolved count", () => {
    expect(unresolvedGapCount([gap({ status: "open" }), gap({ status: "researching" }), gap({ status: "proposed" }), gap({ status: "asked" }),
      gap({ status: "answered" }), gap({ status: "dismissed" })])).toBe(4);
    expect(unresolvedGapCount(null)).toBe(0);
  });
  test("asked N×", () => {
    expect(askedLabel(3)).toBe("asked 3×");
    expect(askedLabel(0)).toBe("asked 1×");
  });
});

describe("consolidating", () => {
  test("a line per stage, none when idle", () => {
    expect(consolidatingLine(null)).toBe("");
    expect(consolidatingLine({ running: false, stage: "lessons", gap: null })).toBe("");
    expect(consolidatingLine({ running: true, stage: "lessons", gap: null })).toBe("Consolidating lessons…");
    expect(consolidatingLine({ running: true, stage: "gaps", gap: null })).toBe("Sorting open questions…");
    expect(consolidatingLine({ running: true, stage: "researching", gap: "g" })).toBe("Researching an open question…");
    expect(consolidatingLine({ running: true, stage: "proposing", gap: "g" })).toBe("Drafting a skill fix…");
  });
});

const obs = (p: Partial<ExpertObs>): ExpertObs => ({ id: "o", ts: 0, kind: "user_correction", claim: "c", trust: "user", session: "", about: [], consult: "", ...p });

describe("observations", () => {
  test("type tags", () => {
    expect(observationTypeLabel("gap")).toBe("unsure");
    expect(observationTypeLabel("repeated_request")).toBe("asked for");
    expect(observationTypeLabel("user_correction")).toBe("correction");
    expect(observationTypeLabel("error_then_fix")).toBe("fix that worked");
    expect(observationTypeLabel("answer_contradicted")).toBe("advice checked");
    expect(observationTypeLabel("something_new")).toBe("learning");
    expect(observationTypeLabel(undefined)).toBe("learning");
  });
  test("result tags: the op, or the gap's standing", () => {
    expect(observationResultLabel(obs({ outcome: { op: "add" } }))).toBe("new lesson");
    expect(observationResultLabel(obs({ outcome: { op: "bump" } }))).toBe("counted toward a lesson");
    expect(observationResultLabel(obs({ outcome: { op: "nothing" } }))).toBe("nothing new");
    expect(observationResultLabel(obs({}))).toBe("");
    expect(observationResultLabel(obs({ kind: "gap", outcome: { op: "gap" }, gap: gap({ status: "proposed" }) }))).toBe("skill fix proposed in chat");
    expect(observationResultLabel(obs({ kind: "gap", gap: gap({ id: "x", status: "open" }) }), "x")).toBe("researching…");
    expect(observationResultLabel(obs({ kind: "gap", gap: gap({ status: "answered", resolution: "skill" }) }))).toBe("answered by a new skill");
  });
  test("meta: when, then the source, else consult", () => {
    const now = 1_000_000_000_000;
    expect(observationMeta(obs({ ts: now / 1000, source: { target: "/x/canvases/sales/", backend: "claude" } }), now)).toMatch(/ · sales · claude$/);
    expect(observationMeta(obs({ ts: now / 1000, consult: "c1" }), now)).toMatch(/ · consult$/);
    expect(observationMeta(obs({ ts: now / 1000 }), now)).not.toContain("·");
  });
  test("waiting oldest first, consolidated newest first; missing lists are empty", () => {
    const { waiting, done } = sortObservations([obs({ id: "b", ts: 2 }), obs({ id: "a", ts: 1 })], [obs({ id: "c", ts: 3 }), obs({ id: "d", ts: 5 })]);
    expect(waiting.map((o) => o.id)).toEqual(["a", "b"]);
    expect(done.map((o) => o.id)).toEqual(["d", "c"]);
    expect(sortObservations(undefined, null)).toEqual({ waiting: [], done: [] });
  });
  test("the joined gaps, each once; the gap being researched", () => {
    const g = gap({ id: "g1" });
    expect(observationGaps([obs({ gap: g }), obs({})], [obs({ gap: g })]).map((x) => x.id)).toEqual(["g1"]);
    expect(researchingGap({ running: true, stage: "researching", gap: "g1" })).toBe("g1");
    expect(researchingGap({ running: true, stage: "lessons", gap: null })).toBe(null);
    expect(researchingGap({ running: false, stage: "researching", gap: "g1" })).toBe(null);
  });
});
