import { describe, expect, test } from "bun:test";
import type { ExpertConsult, ExpertEngineRow, ExpertGap, ExpertItem, ExpertObs, ExpertProposal } from "./api";
import { gapNote, gapProposal, observationText } from "./expert";
import { CONSULT_STATUS_LABEL, consultFacts, consultSource, consultStatus, filterConsults, formatDuration, groupConsultsByDay, isLongContext, usedLessons } from "./expert";
import { filterLessons, filterObservations, LESSON_STATUS_LABEL, lessonFacts, lessonSource, lessonStatus, observationFacts, observationSource, observationTone } from "./expert";
import { anyPreparing, askedLabel, basename, consolidatingLine, gapStatusLabel, observationGaps, observationResultLabel, observationTypeLabel,
  researchingGap, sortObservations, unresolvedGapCount, checkProgress, engineNote, engineOptions, modelOptions, openProposals, proposalLine, proposalText, proposalWarnings, warningsLine, scoreText,
  scoresLine, sortConsults, sortLessons } from "./expert";
import { budgetState, ENGINE_HELP, lessonUses, removedReasonLabel, tidySummary, usedLabel, usesLine } from "./expert";

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
  test("most used first: sessions seen plus answers relied on", () => {
    const out = sortLessons([item({ id: "a", count: 3 }), item({ id: "b", count: 1, cited: 4 })]);
    expect(out.map((x) => x.id)).toEqual(["b", "a"]);
  });
  test("lessonUses: count + cited (cited absent reads 0)", () => {
    expect(lessonUses(item({ count: 2, cited: 3 }))).toBe(5);
    expect(lessonUses(item({ count: 2 }))).toBe(2);
    expect(lessonUses({ count: undefined as unknown as number, cited: undefined })).toBe(0);
  });
  test("used N×; the opened card's line", () => {
    expect(usedLabel(item({ count: 2, cited: 3 }))).toBe("used 5×");
    expect(usedLabel(item({ count: 0 }))).toBe("used 1×");
    expect(usesLine(item({ count: 2, cited: 1 }))).toBe("Seen in 2 sessions · relied on in 1 answer");
    expect(usesLine(item({ count: 1 }))).toBe("Seen in 1 session");
  });
  test("budgetState: null without a limit; the sentence and the meter by state", () => {
    expect(budgetState(null)).toBeNull();
    expect(budgetState({ tidy_at: 30, total: 3 })).toBeNull();
    expect(budgetState({ limit: 0, total: 3 })).toBeNull();
    const below = budgetState({ limit: 50, tidy_at: 30, total: 30 })!;
    expect(below).toMatchObject({ label: "30 of 50 lessons", above: false, note: "Tidying starts above 30.", fill: 60, over: 0 });
    const above = budgetState({ limit: 50, tidy_at: 30, total: 32 })!;
    expect(above).toMatchObject({ label: "32 of 50 lessons", above: true, fill: 60, over: 4 });
    expect(above.note).toBe("Above 30: each consolidation removes old, rarely used lessons, merges related ones, and proposes heavily used ones as skills.");
    // total missing: the lessons listed; tidy_at missing: the limit; over the limit: the bar is the total.
    expect(budgetState({ limit: 50 }, 7)).toMatchObject({ total: 7, tidyAt: 50, note: "Tidying starts above 50." });
    expect(budgetState({ limit: 50, tidy_at: 30, total: 60 })).toMatchObject({ fill: 50, over: 50 });
  });
  test("removedReasonLabel", () => {
    expect(removedReasonLabel({ reason: "stale" })).toBe("removed: old and rarely used");
    expect(removedReasonLabel({ reason: "promoted", skill: "h3-maps" })).toBe("moved into the skill h3-maps");
    expect(removedReasonLabel({ reason: "promoted" })).toBe("moved into a skill");
    expect(removedReasonLabel({ reason: "over_limit" })).toBe("removed: memory was over its limit");
    expect(removedReasonLabel({ reason: "forgotten" })).toBe("forgotten by you");
    expect(removedReasonLabel({ reason: "whatever" })).toBe("removed");
    const items = [item({ id: "n", claim: "Use H3 for all hex maps" })];
    expect(removedReasonLabel({ reason: "merged", into: "n" }, items)).toBe("merged into: Use H3 for all hex maps");
    expect(removedReasonLabel({ reason: "merged", into: "gone" }, items)).toBe("merged into: another lesson");
    expect(removedReasonLabel({ reason: "merged" })).toBe("merged into: another lesson");
  });
  test("tidySummary: under a day old, zero parts left out", () => {
    const now = 1_800_000_000_000, at = now / 1000 - 3600, r = (id: string) => ({ id, claim: id });
    expect(tidySummary(null, now)).toBe("");
    expect(tidySummary({ at, deleted: [r("a"), r("b")], promoting: [r("c")], over_limit: [r("d")] }, now)).toBe("Last tidy: removed 3, proposing 1 as skill.");
    expect(tidySummary({ at, deleted: [r("a")], promoting: [] , over_limit: [] }, now)).toBe("Last tidy: removed 1.");
    expect(tidySummary({ at, promoting: [r("a"), r("b")] }, now)).toBe("Last tidy: proposing 2 as skills.");
    expect(tidySummary({ at, deleted: [], promoting: [], over_limit: [] }, now)).toBe("");
    expect(tidySummary({ at: now / 1000 - 86400, deleted: [r("a")] }, now)).toBe("");
    const merged = [{ into: "n1", claim: "N1", from: [r("a"), r("b")] }, { into: "n2", claim: "N2", from: [r("c"), r("d"), r("e")] }];
    expect(tidySummary({ at, deleted: [r("x")], merged, promoting: [r("p")] }, now)).toBe("Last tidy: removed 1, merged 5 into 2, proposing 1 as skill.");
    expect(tidySummary({ at, merged: [{ into: "n", claim: "N", from: [r("a"), r("b")] }] }, now)).toBe("Last tidy: merged 2 into 1.");
    expect(tidySummary({ at, merged: [] }, now)).toBe("");
  });
  test("status: contradicted wins, then in use / not used yet", () => {
    expect(lessonStatus(item({}))).toBe("in-use");
    expect(lessonStatus(item({ indexed: true }))).toBe("in-use");
    expect(lessonStatus(item({ indexed: false }))).toBe("not-used");
    expect(lessonStatus(item({ indexed: false, contradicted: 1 }))).toBe("contradicted");
    expect(LESSON_STATUS_LABEL["not-used"]).toBe("Not used yet");
  });
  test("source by trust", () => {
    expect(lessonSource(item({ trust: "user" }))).toBe("taught by you");
    expect(lessonSource(item({ trust: "outcome" }))).toBe("from sessions");
    expect(lessonSource(item({ trust: "research" }))).toBe("found by research");
    expect(lessonSource(item({ trust: "gap" }))).toBe("");
  });
  test("facts: skill state, contradicted, why not used", () => {
    expect(lessonFacts(item({}))).toEqual([]);
    // An older server's promoted lesson: no skill fact (a lesson in a skill now leaves memory).
    expect(lessonFacts(item({ promoted: true, proposed: true }))).toEqual([]);
    expect(lessonFacts(item({ proposed: true }))).toEqual(["skill proposal pending"]);
    expect(lessonFacts(item({ proposed: true, rejected: true }))).toEqual(["not proposed again"]);
    expect(lessonFacts(item({ contradicted: 2 }))).toEqual(["contradicted 2×"]);
    expect(lessonFacts(item({ merged_from: [{ id: "a", claim: "A" }, { id: "b", claim: "B" }] }))).toEqual(["merged from 2"]);
    expect(lessonFacts(item({ merged_from: [] }))).toEqual([]);
    expect(lessonFacts(item({ indexed: false }))).toEqual(["seen once"]);
    expect(lessonFacts(item({ trust: "research", indexed: false, proposed: true }))).toEqual(["skill proposal pending", "waiting for your approval"]);
  });
  test("filter: in use (contradicted included) and not used yet, order kept", () => {
    const xs = [item({ id: "a", indexed: false }), item({ id: "b" }), item({ id: "c", contradicted: 1 }), item({ id: "d", indexed: true })];
    expect(filterLessons(xs, "all").map((x) => x.id)).toEqual(["a", "b", "c", "d"]);
    expect(filterLessons(xs, "in-use").map((x) => x.id)).toEqual(["b", "c", "d"]);
    expect(filterLessons(xs, "not-used").map((x) => x.id)).toEqual(["a"]);
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
});

describe("consult cards", () => {
  const c = (p: Partial<ExpertConsult>): ExpertConsult => ({ id: "c", ts: 0, question: "", context: "", answer: "", refs: [], used: [], unsure: false, ms: 0, ...p });
  test("status: answered unless unsure", () => {
    expect(consultStatus(c({}))).toBe("answered");
    expect(consultStatus(c({ unsure: true }))).toBe("unsure");
    expect(CONSULT_STATUS_LABEL.unsure).toBe("Unsure");
  });
  test("source: target basename and backend, or direct", () => {
    expect(consultSource(c({ caller: { target: "/u/canvases/Airbnb_AI_Visualization/", backend: "claude" } }))).toBe("Airbnb_AI_Visualization · claude");
    expect(consultSource(c({ caller: { target: "/u/x/sales" } }))).toBe("sales");
    expect(consultSource(c({ caller: { backend: "codex" } }))).toBe("direct");
    expect(consultSource(c({ caller: null }))).toBe("direct");
  });
  test("duration: seconds, then minutes and seconds", () => {
    expect(formatDuration(18_200)).toBe("18s");
    expect(formatDuration(65_000)).toBe("1m 5s");
    expect(formatDuration(120_000)).toBe("2m");
    expect(formatDuration(300)).toBe("<1s");
    expect(formatDuration(0)).toBe("");
    expect(formatDuration(undefined)).toBe("");
  });
  test("facts: files, lessons used, duration, each only when present", () => {
    expect(consultFacts(c({ refs: ["a", "b"], used: ["m1"], ms: 18_000 }))).toEqual(["2 files", "1 lesson used", "18s"]);
    expect(consultFacts(c({ refs: ["a"], used: ["m1", "m2"] }))).toEqual(["1 file", "2 lessons used"]);
    expect(consultFacts(c({}))).toEqual([]);
  });
  test("filter by status", () => {
    const cs = [c({ id: "a" }), c({ id: "b", unsure: true }), c({ id: "d" })];
    expect(filterConsults(cs, "all").map((x) => x.id)).toEqual(["a", "b", "d"]);
    expect(filterConsults(cs, "answered").map((x) => x.id)).toEqual(["a", "d"]);
    expect(filterConsults(cs, "unsure").map((x) => x.id)).toEqual(["b"]);
  });
  test("group by day: newest first, Today / Yesterday / short date", () => {
    const now = new Date(2026, 10, 17, 15, 0).getTime(), at = (d: number, h: number) => new Date(2026, 10, d, h).getTime() / 1000;
    const gs = groupConsultsByDay([c({ id: "old", ts: at(10, 9) }), c({ id: "t1", ts: at(17, 9) }), c({ id: "y", ts: at(16, 20) }), c({ id: "t2", ts: at(17, 14) })], now);
    expect(gs.map((g) => g.label)).toEqual(["Today", "Yesterday", new Date(2026, 10, 10).toLocaleDateString([], { month: "short", day: "numeric" })]);
    expect(gs[0].items.map((x) => x.id)).toEqual(["t2", "t1"]);
    expect(groupConsultsByDay([], now)).toEqual([]);
  });
  test("long context and used lessons", () => {
    expect(isLongContext("a\nb\nc\nd")).toBe(false);
    expect(isLongContext("a\nb\nc\nd\ne")).toBe(true);
    expect(isLongContext("x".repeat(300))).toBe(true);
    expect(usedLessons(["m1", "gone"], [item({ id: "m1", claim: "Use bar charts" })])).toEqual([{ id: "m1", text: "Use bar charts" }, { id: "gone", text: "gone" }]);
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
  test("before the engines arrive, the saved engine is still an option (a change never writes Auto by accident)", () => {
    expect(engineOptions([], "claude")).toEqual([["auto", "Auto"], ["claude", "Claude"]]);
    expect(engineOptions([], "copilot")).toEqual([["auto", "Auto"], ["copilot", "Copilot"]]);
    expect(engineOptions([], "codex")).toEqual([["auto", "Auto"], ["codex", "Codex"]]);
    expect(engineOptions([], "auto")).toEqual([["auto", "Auto"]]);
    expect(engineOptions(rows.filter((r) => r.id !== "copilot"), "copilot")).toContainEqual(["copilot", "Copilot"]);
  });
  test("the Auto helper text: Claude, else Copilot, never Codex", () => {
    expect(ENGINE_HELP).toContain("Auto uses Claude when it is on, otherwise Copilot");
    expect(ENGINE_HELP).toContain("never picks Codex");
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

describe("proposals", () => {
  const p = (id: string, status: ExpertProposal["status"], ts: number, error?: string): ExpertProposal => ({ id, ts, status, action: "create", skill: null, title: "",
    trigger: "", body: "", diff: "", items: [], score_before: null, score_after: null, card_seq: null, ...(error ? { error } : {}) });
  test("the open ones newest first, each in its sentence", () => {
    const open = openProposals([p("a", "approved", 5), p("b", "preparing", 1), p("c", "shown", 3), p("d", "failed", 2, "checks dropped"), p("e", "rejected", 9)]);
    expect(open.map((x) => x.id)).toEqual(["c", "d", "b"]);
    expect(proposalLine(p("b", "preparing", 1))).toBe("Drafting a skill proposal…");
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
  test("a proposed gap says where its proposal stands", () => {
    expect(gapStatusLabel(gap({ status: "proposed", proposal_status: "preparing" }))).toBe("drafting a skill fix…");
    expect(gapStatusLabel(gap({ status: "proposed", proposal_status: "ready" }))).toBe("skill fix ready, card coming");
    expect(gapStatusLabel(gap({ status: "proposed", proposal_status: "shown" }))).toBe("skill fix proposed in chat");
    expect(gapStatusLabel(gap({ status: "proposed", proposal_status: "failed", proposal_error: "boom" }))).toBe("skill fix failed");
    expect(gapProposal(gap({ status: "proposed", proposal_status: "failed", proposal_error: "boom" }))).toEqual({ status: "failed", error: "boom" });
    // An older server: the proposal looked up in the fetched list; neither known keeps today's wording.
    const ps = [{ id: "p1", status: "preparing" as const }, { id: "p2", status: "failed" as const, error: "no diff" }];
    expect(gapStatusLabel(gap({ status: "proposed", proposal: "p1" }), null, ps)).toBe("drafting a skill fix…");
    expect(gapProposal(gap({ proposal: "p2" }), ps)).toEqual({ status: "failed", error: "no diff" });
    expect(gapStatusLabel(gap({ status: "proposed", proposal: "zz" }), null, ps)).toBe("skill fix proposed in chat");
    expect(gapStatusLabel(gap({ status: "proposed", proposal: "p1" }))).toBe("skill fix proposed in chat");
    expect(observationResultLabel({ kind: "gap", gap: gap({ status: "proposed", proposal: "p1" }) }, null, ps)).toBe("drafting a skill fix…");
  });
  test("research note and unsure text", () => {
    expect(gapNote("not found: no doc mentions it")).toBe("Looked into it: no doc mentions it");
    expect(gapNote("not found")).toBe("Looked into it: nothing found");
    expect(gapNote("engine timed out")).toBe("engine timed out");
    expect(gapNote("")).toBe("");
    expect(observationText({ kind: "gap", claim: "Could not answer: How do I chain UDFs?" })).toBe("How do I chain UDFs?");
    expect(observationText({ kind: "user_correction", claim: "Could not answer: x" })).toBe("Could not answer: x");
    expect(observationText({ kind: "gap", claim: "", gap: { question: "Q?" } })).toBe("Q?");
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
    expect(consolidatingLine({ running: true, stage: "tidying", gap: null })).toBe("Tidying memory…");
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
    expect(observationTypeLabel("advice_confirmed")).toBe("advice confirmed");
    expect(observationTypeLabel("advice_contradicted")).toBe("advice contradicted");
    // The legacy kind: "advice checked" only while it has no outcome.
    expect(observationTypeLabel("answer_contradicted")).toBe("advice checked");
    expect(observationTypeLabel("answer_contradicted", null)).toBe("advice checked");
    expect(observationTypeLabel("answer_contradicted", { op: "contradict" })).toBe("advice contradicted");
    expect(observationTypeLabel("answer_contradicted", { op: "bump" })).toBe("advice confirmed");
    expect(observationTypeLabel("something_new")).toBe("learning");
    expect(observationTypeLabel(undefined)).toBe("learning");
  });
  test("result tags: the op, or the gap's standing", () => {
    expect(observationResultLabel(obs({ outcome: { op: "add" } }))).toBe("new lesson");
    expect(observationResultLabel(obs({ outcome: { op: "bump" } }))).toBe("counted toward a lesson");
    expect(observationResultLabel(obs({ outcome: { op: "nothing" } }))).toBe("nothing new");
    expect(observationResultLabel(obs({ kind: "advice_contradicted", outcome: { op: "contradict", item: "m1", new: "m2" } }))).toBe("contradicted a lesson");
    expect(observationResultLabel(obs({}))).toBe("");
    expect(observationResultLabel(obs({ kind: "gap", outcome: { op: "gap" }, gap: gap({ status: "proposed" }) }))).toBe("skill fix proposed in chat");
    expect(observationResultLabel(obs({ kind: "gap", gap: gap({ id: "x", status: "open" }) }), "x")).toBe("researching…");
    expect(observationResultLabel(obs({ kind: "gap", gap: gap({ status: "answered", resolution: "skill" }) }))).toBe("answered by a new skill");
  });
  test("source: folder and backend, else consult", () => {
    expect(observationSource(obs({ source: { target: "/x/canvases/sales/", backend: "claude" } }))).toBe("sales · claude");
    expect(observationSource(obs({ consult: "c1" }))).toBe("consult");
    expect(observationSource(obs({}))).toBe("");
  });
  test("type marker tone", () => {
    expect(observationTone("gap")).toBe("warn");
    expect(observationTone("advice_contradicted")).toBe("warn");
    expect(observationTone("answer_contradicted", { op: "contradict" })).toBe("warn");
    expect(observationTone("advice_confirmed")).toBe("ok");
    expect(observationTone("error_then_fix")).toBe("ok");
    expect(observationTone("user_correction")).toBe("accent");
    expect(observationTone("repeated_request")).toBe("muted");
  });
  test("facts: waiting, what came of it, asked N×", () => {
    expect(observationFacts(obs({ kind: "user_correction" }), false)).toEqual(["waiting to be consolidated"]);
    expect(observationFacts(obs({ kind: "gap", gap: gap({ status: "open", count: 3 }) }), false)).toEqual(["waiting to be consolidated", "asked 3×"]);
    expect(observationFacts(obs({ kind: "gap", gap: gap({ id: "x", status: "open" }) }), false, "x")).toEqual(["researching…"]);
    expect(observationFacts(obs({ kind: "gap", gap: gap({ status: "proposed", proposal_status: "preparing" }) }), false)).toEqual(["drafting a skill fix…"]);
    expect(observationFacts(obs({ outcome: { op: "add" } }), true)).toEqual(["new lesson"]);
    expect(observationFacts(obs({ outcome: { op: "weird" } }), true)).toEqual([]);
  });
  test("filter: unsure is the gaps, learnings the rest", () => {
    const os = [obs({ id: "a", kind: "gap" }), obs({ id: "b", kind: "user_correction" }), obs({ id: "c", kind: "advice_confirmed" })];
    expect(filterObservations(os, "all").map((o) => o.id)).toEqual(["a", "b", "c"]);
    expect(filterObservations(os, "unsure").map((o) => o.id)).toEqual(["a"]);
    expect(filterObservations(os, "learnings").map((o) => o.id)).toEqual(["b", "c"]);
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

describe("proposalWarnings / warningsLine", () => {
  test("from the event field, else from the text's Warning lines", () => {
    expect(proposalWarnings(["removes 2 code blocks", "", 3])).toEqual(["removes 2 code blocks"]);
    expect(proposalWarnings(undefined, "Hi.\nWarning: this change shrinks this skill by 40%.\nApprove?")).toEqual(["shrinks this skill by 40%"]);
    expect(proposalWarnings(null, "Hi.\nApprove?")).toEqual([]);
  });
  test("one line, empty for none", () => {
    expect(warningsLine(["a", "b"])).toBe("Heads up: this change a · b");
    expect(warningsLine([])).toBe("");
  });
  test("proposalText drops the warning lines", () => {
    expect(proposalText("Hi.\nWarning: this change removes 1 code block.\nApprove?")).toBe("Hi.");
  });
});

describe("learning wording (corrections, confirmations)", () => {
  test("a user lesson from a session says so; confirmed N× is a fact; corrected removals", () => {
    expect(lessonSource(item({ trust: "user", via: "session" }))).toBe("you said this in a session");
    expect(lessonSource(item({ trust: "user" }))).toBe("taught by you");
    expect(lessonFacts(item({ confirmed: 3 }))).toContain("confirmed 3×");
    expect(lessonFacts(item({ confirmed: 0 })).some((f) => f.startsWith("confirmed"))).toBe(false);
    expect(removedReasonLabel({ reason: "corrected", into: "m2" })).toBe("replaced by your correction");
  });
  test("a confirm outcome does not raise the count", () => {
    expect(observationResultLabel({ kind: "advice_confirmed", outcome: { op: "confirm", item: "m1" } })).toBe("noted as confirmed; does not raise the count");
  });
});
