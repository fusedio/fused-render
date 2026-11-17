import { describe, expect, test } from "bun:test";
import { ApiError, type ExpertActivity, type ExpertConsult, type ExpertEngineRow, type ExpertItem } from "./api";
import { CONSULT_STATUS_LABEL, consultFacts, consultSource, consultStatus, filterConsults, formatDuration, groupConsultsByDay, isLongContext, usedLessons } from "./expert";
import { filterLessons, LESSON_STATUS_LABEL, lessonFacts, lessonSource, lessonStatus } from "./expert";
import { basename, engineNote, engineOptions, modelOptions, sortConsults, sortLessons } from "./expert";
import { budgetState, ENGINE_HELP, LEARNING_HELP, lessonUses, tidySummary, usedLabel, usesLine } from "./expert";
import { actionLabel, actionTone, activityGroup, activityTarget, canUndo, evidenceParts, filterActivity, isQuiet, learningLine, shownActivity, undoError, waitingLine } from "./expert";
import { unreadByExpert } from "./expertUnread";

const item = (p: Partial<ExpertItem>): ExpertItem => ({ id: "m", claim: "c", kind: "lesson", trust: "outcome", count: 1, sessions: [], about: [],
  created: 0, updated: 0, ...p });

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
    expect(above.note).toBe("Above 30: it removes old, rarely used lessons, merges related ones, and moves heavily used ones into skills.");
    // total missing: the lessons listed; tidy_at missing: the limit; over the limit: the bar is the total.
    expect(budgetState({ limit: 50 }, 7)).toMatchObject({ total: 7, tidyAt: 50, note: "Tidying starts above 50." });
    expect(budgetState({ limit: 50, tidy_at: 30, total: 60 })).toMatchObject({ fill: 50, over: 50 });
  });
  test("tidySummary: under a day old, zero parts left out", () => {
    const now = 1_800_000_000_000, at = now / 1000 - 3600, r = (id: string) => ({ id, claim: id });
    expect(tidySummary(null, now)).toBe("");
    expect(tidySummary({ at, deleted: [r("a"), r("b")], promoting: [r("c")], over_limit: [r("d")] }, now)).toBe("Last tidy: removed 3, moved 1 into skill.");
    expect(tidySummary({ at, deleted: [r("a")], promoting: [] , over_limit: [] }, now)).toBe("Last tidy: removed 1.");
    expect(tidySummary({ at, promoting: [r("a"), r("b")] }, now)).toBe("Last tidy: moved 2 into skills.");
    expect(tidySummary({ at, deleted: [], promoting: [], over_limit: [] }, now)).toBe("");
    expect(tidySummary({ at: now / 1000 - 86400, deleted: [r("a")] }, now)).toBe("");
    const merged = [{ into: "n1", claim: "N1", from: [r("a"), r("b")] }, { into: "n2", claim: "N2", from: [r("c"), r("d"), r("e")] }];
    expect(tidySummary({ at, deleted: [r("x")], merged, promoting: [r("p")] }, now)).toBe("Last tidy: removed 1, merged 5 into 2, moved 1 into skill.");
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
  test("facts: merged, contradicted, confirmed, seen once; nothing about skills", () => {
    expect(lessonFacts(item({}))).toEqual([]);
    expect(lessonFacts(item({ contradicted: 2 }))).toEqual(["contradicted 2×"]);
    expect(lessonFacts(item({ merged_from: [{ id: "a", claim: "A" }, { id: "b", claim: "B" }] }))).toEqual(["merged from 2"]);
    expect(lessonFacts(item({ merged_from: [] }))).toEqual([]);
    expect(lessonFacts(item({ indexed: false }))).toEqual(["seen once"]);
    expect(lessonFacts(item({ trust: "research", indexed: false }))).toEqual(["seen once"]);
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

describe("activity", () => {
  const line = (p: Partial<ExpertActivity>): ExpertActivity => ({ id: "a", ts: 1, action: "learned", summary: "s", ok: true, ...p });
  test("every action in plain words; an unknown one reads as itself", () => {
    const want: Record<string, string> = { learned: "Learned", counted: "Seen again", corrected: "Corrected", forgot: "Forgot", merged: "Merged", removed: "Removed",
      skill_updated: "Skill updated", skill_created: "Skill created", skill_restored: "Skill restored", noticed: "Noticed", researched: "Researched",
      could_not_find: "Could not find out", skipped: "Skipped", undo: "Undo" };
    for (const [a, l] of Object.entries(want)) expect(actionLabel(a)).toBe(l);
    expect(actionLabel("lesson_split")).toBe("Lesson split");
    expect(actionLabel("")).toBe("Did something");
  });
  test("tone: a failure and a dead end warn, skills are accented, undone is muted", () => {
    expect(actionTone(line({ action: "learned" }))).toBe("ok");
    expect(actionTone(line({ action: "skill_updated" }))).toBe("accent");
    expect(actionTone(line({ action: "skill_updated", undone: true }))).toBe("muted");
    expect(actionTone(line({ action: "skill_created", ok: false }))).toBe("warn");
    expect(actionTone(line({ action: "could_not_find" }))).toBe("warn");
    expect(actionTone(line({ action: "forgot" }))).toBe("muted");
  });
  test("the header names a skill, never an internal id", () => {
    for (const action of ["skill_updated", "skill_created", "skill_restored"]) expect(activityTarget(line({ action, target: " maps.md " }))).toBe("maps.md");
    expect(activityTarget(line({ action: "learned", target: "m64e724f4" }))).toBe("");
    expect(activityTarget(line({ action: "undo", target: "a62b0e4a3" }))).toBe("");
    expect(activityTarget(line({ action: "skill_updated" }))).toBe("");
  });
  test("a dead end reads plainly and sits under All only", () => {
    const a = line({ action: "could_not_find" });
    expect(actionLabel(a.action)).toBe("Could not find out");
    expect(activityGroup(a)).toBe("");
    expect(filterActivity([a], "skills")).toEqual([]);
    expect(filterActivity([a], "memory")).toEqual([]);
    expect(filterActivity([a], "all")).toEqual([a]);
  });
  test("the Skills and Memory chips; notes and undos only under All", () => {
    const xs = ["learned", "skill_updated", "skipped", "undo", "merged", "skill_restored", "researched"].map((action, i) => line({ id: String(i), action }));
    expect(xs.map(activityGroup)).toEqual(["memory", "skills", "", "", "memory", "skills", ""]);
    expect(filterActivity(xs, "skills").map((a) => a.id)).toEqual(["1", "5"]);
    expect(filterActivity(xs, "memory").map((a) => a.id)).toEqual(["0", "4"]);
    expect(filterActivity(xs, "all")).toHaveLength(7);
  });
  test("quiet lines: the flag, else counted and noticed; hidden unless everything; newest first", () => {
    expect(isQuiet(line({ quiet: true }))).toBe(true);
    expect(isQuiet(line({ action: "counted" }))).toBe(true);
    expect(isQuiet(line({ action: "noticed", quiet: false }))).toBe(false);
    expect(isQuiet(line({}))).toBe(false);
    const xs = [line({ id: "a", ts: 1 }), line({ id: "b", ts: 3, quiet: true }), line({ id: "c", ts: 2 })];
    expect(shownActivity(xs, false).map((a) => a.id)).toEqual(["c", "a"]);
    expect(shownActivity(xs, true).map((a) => a.id)).toEqual(["b", "c", "a"]);
    expect(xs[0].id).toBe("a");
  });
  test("Undo: only a change that worked and was not taken back, never an undo", () => {
    expect(canUndo(line({ undoable: true, undone: false }))).toBe(true);
    expect(canUndo(line({ undoable: true, undone: true }))).toBe(false);
    expect(canUndo(line({ undoable: false }))).toBe(false);
    expect(canUndo(line({ undoable: true, ok: false }))).toBe(false);
    expect(canUndo(line({ action: "undo", undoable: true }))).toBe(false);
  });
  test("what waits, and the step while it learns", () => {
    expect(waitingLine(0)).toBe("");
    expect(waitingLine(undefined)).toBe("");
    expect(waitingLine(1)).toBe("1 thing waiting to be learned");
    expect(waitingLine(4)).toBe("4 things waiting to be learned");
    expect(learningLine(null)).toBe("");
    expect(learningLine({ running: false, stage: "tidy" })).toBe("");
    expect(learningLine({ running: true, stage: "lessons" })).toBe("Going through what it noticed…");
    expect(learningLine({ running: true, stage: "research" })).toBe("Looking into open questions…");
    expect(learningLine({ running: true, stage: "distill" })).toBe("Updating skills…");
    expect(learningLine({ running: true, stage: "tidy" })).toBe("Tidying memory…");
    expect(learningLine({ running: true, stage: "" })).toBe("Learning…");
  });
  test("evidence: the quote, where it came from, the file", () => {
    expect(evidenceParts(null)).toEqual({ quote: "", from: "", fromTitle: "", file: "" });
    expect(evidenceParts({ quote: " use H3 ", session: "s1", source: { target: "/x/canvases/Airbnb/", backend: "claude" } }))
      .toEqual({ quote: "use H3", from: "Airbnb · claude", fromTitle: "/x/canvases/Airbnb/\nsession s1", file: "" });
    expect(evidenceParts({ source: { canvas: "/x/canvases/Maps", target: "/x/t" } }).from).toBe("Maps");
    expect(evidenceParts({ session: "s1" }).from).toBe("a session");
    expect(evidenceParts({ source: "/x/docs/tiles.md" })).toMatchObject({ file: "/x/docs/tiles.md", from: "" });
  });
  test("a refused undo: the server's detail, else the error's text", () => {
    expect(undoError(new ApiError("409 Conflict", 409, { detail: "changed since; undo the later change first" }))).toBe("changed since; undo the later change first");
    expect(undoError(new ApiError("boom", 500, null))).toBe("boom");
    expect(undoError(new Error("offline"))).toBe("offline");
  });
  test("the unread dot's counts: only experts with something new", () => {
    expect(unreadByExpert([{ id: "a", unread: 2 }, { id: "b", unread: 0 }, { id: "c" }])).toEqual({ a: 2 });
    expect(unreadByExpert(null)).toEqual({});
  });
  test("the Learning switch's help", () => {
    expect(LEARNING_HELP).toBe("Off: it still answers and notes what happens, and changes nothing.");
  });
});

describe("learning wording (corrections, confirmations)", () => {
  test("a user lesson from a session says so; confirmed N× is a fact", () => {
    expect(lessonSource(item({ trust: "user", via: "session" }))).toBe("you said this in a session");
    expect(lessonSource(item({ trust: "user" }))).toBe("taught by you");
    expect(lessonFacts(item({ confirmed: 3 }))).toContain("confirmed 3×");
    expect(lessonFacts(item({ confirmed: 0 })).some((f) => f.startsWith("confirmed"))).toBe(false);
  });
});
