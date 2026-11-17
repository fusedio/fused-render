import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import type { Bot, ExpertActivity, ExpertActivityReply, ExpertConsult, ExpertItem } from "../lib/api";

const { ActivityPanel, ActivityCard, MemoryPanel, LessonCard, ConsultsPanel, ConsultCard, fromMemory, mergeActivity, appendActivity, ExpertPanels } = await import("./ExpertPane");
const { expertApi } = await import("../lib/api");

const item = (p: Partial<ExpertItem>): ExpertItem => ({ id: "m", claim: "c", kind: "lesson", trust: "outcome", count: 1, sessions: [], about: [],
  created: 0, updated: 0, ...p });
const noop = () => {};

describe("ActivityPanel", () => {
  const now = new Date(2026, 10, 17, 15, 0).getTime(), at = (d: number, h: number) => new Date(2026, 10, d, h).getTime() / 1000;
  const line = (p: Partial<ExpertActivity>): ExpertActivity => ({ id: "a", ts: at(17, 14), action: "learned", target: "", summary: "Use H3 for hex maps.",
    why: "You said so in a task.", ok: true, undoable: true, undone: false, ...p });
  const base = { onEverything: noop, onLearn: noop, onUndo: noop };
  const lines = [
    line({ id: "a1", ts: at(17, 14), action: "skill_updated", target: "maps.md", summary: "Added a rule about hex sizes.", why: "Three sessions needed it." }),
    line({ id: "a2", ts: at(17, 9), action: "learned" }),
    line({ id: "a3", ts: at(16, 20), action: "could_not_find", summary: "Which tile server to use.", why: "No file says.", undoable: false }),
    line({ id: "a4", ts: at(16, 18), action: "counted", summary: "Seen once more.", quiet: true, undoable: false }),
    line({ id: "a5", ts: at(12, 8), action: "forgot", summary: "Old colour rule.", undone: true }),
    line({ id: "a6", ts: at(12, 7), action: "skill_created", summary: "A new skill.", ok: false, error: "the file exists", undoable: false }),
  ];
  test("newest first under day dividers; label, time, summary and why; Undo only where it can be taken back", () => {
    const out = renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, nowMs: now }));
    expect(out.indexOf(">Today<")).toBeLessThan(out.indexOf(">Yesterday<"));
    expect(out.indexOf(">Yesterday<")).toBeLessThan(out.indexOf(">Nov 12<"));
    expect(out.indexOf("Added a rule about hex sizes.")).toBeLessThan(out.indexOf("Use H3 for hex maps."));
    expect(out).toContain(">Skill updated<");
    expect(out).toContain(">Learned<");
    expect(out).toContain(">Could not find out<");
    expect(out).toContain(">Forgot<");
    expect(out).toContain(">maps.md<");
    expect(out).toContain(">1 h ago<");
    expect(out).toContain(">Three sessions needed it.<");
    expect(out).toContain('aria-expanded="false"');
    // a1 and a2 can be undone; a3 is a note, a5 is undone already, a6 failed.
    expect(out.match(/>Undo</g)?.length).toBe(2);
    expect(out).toContain(">undone<");
    expect(out).toContain("This did not work: the file exists");
    // Quiet lines stay out until asked for.
    expect(out).not.toContain("Seen once more.");
    expect(out).toMatch(/All <span class="xp-n">5</);
    expect(out).toMatch(/Skills <span class="xp-n">2</);
    expect(out).toMatch(/Memory <span class="xp-n">2</);
    expect(out).toContain('aria-pressed="false" title="Also show the small things');
    expect(out).not.toMatch(/consolidat|inbox|proposal/i);
  });
  test("Show everything brings the quiet lines in", () => {
    const out = renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, everything: true, nowMs: now }));
    expect(out).toContain("Seen once more.");
    expect(out).toContain(">Seen again<");
    expect(out).toMatch(/All <span class="xp-n">6</);
    expect(out).toMatch(/Memory <span class="xp-n">3</);
  });
  test("on top: what waits, Learn now; while it learns the step and a waiting button", () => {
    const idle = renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, waiting: 3, nowMs: now }));
    expect(idle).toContain("3 things waiting to be learned");
    expect(idle).toContain(">Learn now<");
    expect(idle).not.toContain("disabled");
    expect(idle.indexOf("3 things waiting")).toBeLessThan(idle.indexOf("xp-filters"));
    expect(renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, waiting: 1 }))).toContain("1 thing waiting to be learned");
    expect(renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, waiting: 0 }))).toContain("Nothing is waiting to be learned.");
    const run = renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, waiting: 2, learning: { running: true, stage: "distill" }, nowMs: now }));
    expect(run).toContain("Updating skills…");
    expect(run).toContain("2 things waiting to be learned");
    expect(run).toMatch(/<button type="button" class="busy" disabled=""[^>]*>Learning…</);
    expect(run).not.toContain(">Learn now<");
    expect(renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, busy: true }))).toContain(">Learning…<");
  });
  test("a refused undo says why on its own card; the one being undone waits", () => {
    const out = renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, nowMs: now, undoing: "a2",
      undoErrors: { a1: "changed since; undo the later change first" } }));
    expect(out).toMatch(/role="alert">changed since; undo the later change first</);
    expect(out.indexOf("changed since")).toBeLessThan(out.indexOf("Use H3 for hex maps."));
    expect(out).toMatch(/class="xp-undo busy" disabled=""/);
  });
  test("phone: no Learn now, no Undo; learning off says so; more to load; empty and loading", () => {
    const lan = renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, lan: true, nowMs: now }));
    expect(lan).not.toContain(">Learn now<");
    expect(lan).not.toContain(">Undo<");
    const off = renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, off: true, waiting: 2 }));
    expect(off).toContain("Learning is off.");
    expect(off).not.toContain(">Learn now<");
    expect(renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines, more: true, onMore: noop }))).toContain(">Show earlier<");
    expect(renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: lines }))).not.toContain(">Show earlier<");
    expect(renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: [] }))).toContain("Nothing yet.");
    expect(renderToStaticMarkup(createElement(ActivityPanel, { ...base, items: null }))).toContain("Loading…");
  });
  test("opened card: your words, where it came from, the file", () => {
    const a = line({ evidence: { quote: "always use H3", session: "s1", source: { target: "/x/canvases/Airbnb/", backend: "claude" } } });
    const out = renderToStaticMarkup(createElement(ActivityCard, { a, onUndo: noop, initialOpen: true, nowMs: now }));
    expect(out).toContain('aria-expanded="true"');
    expect(out).toContain(">You said<");
    expect(out).toContain("“always use H3”");
    expect(out).toContain(">From one of your tasks<");
    expect(out).not.toContain(">Found in<");
    expect(out).not.toContain(">Files<");
    const file = renderToStaticMarkup(createElement(ActivityCard, { a: line({ action: "researched", evidence: { source: "/x/docs/tiles.md" } }), initialOpen: true }));
    expect(file).toContain(">Found in<");
    expect(file).not.toContain("From ");
    expect(file).toContain(">tiles.md<");
    expect(file).not.toContain(">You said<");
    expect(file).not.toContain(">Undo<");  // no handler (the phone)
    expect(renderToStaticMarkup(createElement(ActivityCard, { a: line({ evidence: null }), initialOpen: true }))).toContain("Nothing more is recorded for this.");
    // A task entry: one line naming the canvas, its files as chips; no "Came from", "Read in" or bare "a session".
    const task = renderToStaticMarkup(createElement(ActivityCard, { initialOpen: true, a: line({ action: "noticed",
      why: "This worked in a task. Not used in answers until a second task shows it.",
      evidence: { quote: "show YC company data", session: "s1", source: { canvas: "/x/canvases/Airbnb_AI_Visualization", target: "/x/t", backend: "claude", files: ["/x/t/yc.py", "/x/t/map.html"] } } }) }));
    expect(task).toContain(">Noted<");
    expect(task).not.toContain(">Learned<");
    expect(task).toContain("tone-muted");
    expect(task).not.toContain("tone-ok");
    expect(task).toContain("act-memory");
    expect(task).toContain(">This worked in a task. Not used in answers until a second task shows it.<");
    expect(task).toContain(">You said<");
    expect(task).toContain(">From your task on Airbnb_AI_Visualization<");
    expect(task).toContain(">Files<");
    expect(task).toContain(">yc.py<");
    expect(task).toContain(">map.html<");
    for (const gone of ["Came from", "Read in", "a session", "claude"]) expect(task).not.toContain(gone);
    // Closed: none of the evidence.
    expect(renderToStaticMarkup(createElement(ActivityCard, { a }))).not.toContain("always use H3");
  });
  test("an undo line is never itself undoable", () => {
    const out = renderToStaticMarkup(createElement(ActivityCard, { a: line({ action: "undo", summary: "Put the lesson back.", undoable: true }), onUndo: noop }));
    expect(out).toContain(">Undo<");
    expect(out).not.toContain(">Undone<");
    expect(out).not.toContain("xp-undo");
  });
  test("the header shows a skill's name, and no internal id for a lesson or an undo", () => {
    const card = (p: Partial<ExpertActivity>) => renderToStaticMarkup(createElement(ActivityCard, { a: line(p) }));
    expect(card({ action: "skill_updated", target: "maps.md" })).toContain(">maps.md<");
    expect(card({ action: "learned", target: "m64e724f4" })).not.toContain("m64e724f4");
    const undo = card({ action: "undo", target: "a62b0e4a3", summary: "Undid: Use H3 for hex maps.", undoable: false });
    expect(undo).not.toContain("a62b0e4a3");
    expect(undo).toContain("Undid: Use H3 for hex maps.");
    // The original line keeps its own marker.
    expect(card({ action: "forgot", undone: true })).toContain(">undone<");
  });
});

describe("activity data", () => {
  const l = (id: string, ts: number): ExpertActivity => ({ id, ts, action: "learned", summary: id, ok: true });
  test("a fresh first page lies over the older lines already loaded", () => {
    expect(mergeActivity([l("d", 4), l("c", 3)], [l("c", 3), l("b", 2), l("a", 1)]).map((x) => x.id)).toEqual(["d", "c", "b", "a"]);
    expect(mergeActivity([l("b", 2)], null).map((x) => x.id)).toEqual(["b"]);
  });
  test("a fresh page that shares no line with the loaded ones replaces them (no hole to page past)", () => {
    // Loaded 4..1; a page's worth and more arrived since, so the fresh page is 9..8 and 7..5 are on neither.
    expect(mergeActivity([l("i", 9), l("h", 8)], [l("d", 4), l("c", 3), l("b", 2), l("a", 1)]).map((x) => x.id)).toEqual(["i", "h"]);
    expect(mergeActivity([], [l("a", 1)])).toEqual([]);
  });
  test("an older page goes under the list only while the list still ends where it was asked from", () => {
    const had = [l("d", 4), l("c", 3)];
    expect(appendActivity(had, [l("c", 3), l("b", 2)], "c")).toEqual({ activity: [l("d", 4), l("c", 3), l("b", 2)], more: false });
    expect(appendActivity(had, Array.from({ length: 100 }, (_, i) => l(`o${i}`, 2)), "c")?.more).toBe(true);
    // The list started again since (a refresh without overlap, another filter): the page is not for it.
    expect(appendActivity([l("i", 9), l("h", 8)], [l("b", 2)], "c")).toBeNull();
    expect(appendActivity(null, [l("b", 2)], "c")).toBeNull();
  });
  test("the memory reply: the budget object, or the same fields beside the lessons", () => {
    expect(fromMemory({ items: [item({})], budget: { limit: 50, tidy_at: 30, total: 1 } }).budget).toMatchObject({ limit: 50, tidy_at: 30, total: 1, last_tidy: null });
    expect(fromMemory({ items: [item({})], limit: 40, tidy_at: 20 }).budget).toMatchObject({ limit: 40, tidy_at: 20, total: 1 });
  });
});

describe("MemoryPanel", () => {
  const now = new Date(2026, 10, 17, 15, 0).getTime();
  test("lesson cards most used first: status, source, used N×, claim, facts; never a Forget button", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ id: "a", claim: "Alpha", count: 1, indexed: false }),
      item({ id: "b", claim: "Beta", count: 2, cited: 3, trust: "user" }), item({ id: "c", claim: "Gamma", count: 2, contradicted: 1 })] }));
    expect(out.indexOf("Beta")).toBeLessThan(out.indexOf("Gamma"));
    expect(out.indexOf("Gamma")).toBeLessThan(out.indexOf("Alpha"));
    expect(out).toContain('aria-expanded="false"');
    expect(out).toContain(">In use<");
    expect(out).toContain(">Not used yet<");
    expect(out).toContain(">Contradicted<");
    expect(out).toContain("tone-warn");
    expect(out).toContain(">taught by you<");
    expect(out).toContain(">from tasks<");
    expect(out).toContain("used 5×");
    expect(out).not.toContain("seen 5×");
    expect(out).toContain(">contradicted 1×<");
    expect(out).toContain(">seen once<");
    expect(out).not.toContain(">Forget<");
    expect(out).toMatch(/All <span class="xp-n">3</);
    expect(out).toMatch(/In use <span class="xp-n">2</);
    expect(out).toMatch(/Not used yet <span class="xp-n">1</);
    expect(out).not.toContain("Not used in answers yet");
    expect(out).not.toMatch(/consolidat|proposal|Recently removed/i);
    // No budget sent: no budget line.
    expect(out).not.toContain("xp-budget");
  });
  test("budget line on top: count, meter, the sentence for the state; the last tidy when recent", () => {
    const items = [item({ id: "a", claim: "Alpha" }), item({ id: "b", claim: "Beta" })];
    const below = renderToStaticMarkup(createElement(MemoryPanel, { items, nowMs: now, budget: { limit: 50, tidy_at: 30, total: 12, last_tidy: null } }));
    expect(below).toContain("12 of 50 lessons");
    expect(below).toContain("Tidying starts above 30.");
    expect(below).toContain('role="meter"');
    expect(below).not.toContain("xp-meter-over");
    expect(below).not.toContain("Last tidy");
    expect(below.indexOf("12 of 50 lessons")).toBeLessThan(below.indexOf("xp-filters"));
    const above = renderToStaticMarkup(createElement(MemoryPanel, { items, nowMs: now,
      budget: { limit: 50, tidy_at: 30, total: 32, last_tidy: { at: now / 1000 - 3600, deleted: [{ id: "x", claim: "X" }], promoting: [{ id: "y", claim: "Y" }, { id: "z", claim: "Z" }], over_limit: [] } } }));
    expect(above).toContain("32 of 50 lessons");
    expect(above).toContain("Above 30: it removes old, rarely used lessons, merges related ones, and moves heavily used ones into skills.");
    expect(above).toContain("xp-meter-over");
    expect(above).toContain("Last tidy: removed 1, moved 2 into skills.");
    // No limit: no line at all.
    expect(renderToStaticMarkup(createElement(MemoryPanel, { items, budget: { tidy_at: 30, total: 2 } }))).not.toContain("xp-budget");
  });
  test("opened lesson: about chips, sessions / answers, first seen / last used; no Forget (it is taught in chat)", () => {
    const it = item({ id: "a", claim: "Use H3", about: ["maps", "h3"], count: 2, cited: 4, created: now / 1000 - 3 * 86400, updated: now / 1000 - 7200,
      last_used: now / 1000 - 600, sessions: ["s1", "s2"] });
    const out = renderToStaticMarkup(createElement(LessonCard, { it, initialOpen: true, nowMs: now }));
    expect(out).toContain('aria-expanded="true"');
    expect(out).toContain(">About<");
    expect(out).toContain(">maps<");
    expect(out).toContain("Seen in 2 tasks · relied on in 4 answers");
    expect(out).toContain("first seen 3 d ago · last used 10 min ago");
    // An older server (no cited / last_used): the sessions alone and the last seen time.
    const old = renderToStaticMarkup(createElement(LessonCard, { it: item({ count: 1, updated: now / 1000 - 7200 }), initialOpen: true, nowMs: now }));
    expect(old).toContain("Seen in 1 task<");
    expect(old).toContain("last seen 2 h ago");
    expect(out).not.toContain(">Forget<");
    expect(out).not.toContain("Merged from");
    const m = renderToStaticMarkup(createElement(LessonCard, { it: item({ merged_from: [{ id: "a", claim: "Orig one" }, { id: "b", claim: "Orig two" }] }),
      initialOpen: true, nowMs: now }));
    expect(m).toContain(">merged from 2<");
    expect(m).toContain(">Merged from<");
    expect(m).toContain("<li>Orig one</li><li>Orig two</li>");
  });
  test("a single lesson has no filter row", () => {
    expect(renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ trust: "research", indexed: false })] }))).not.toContain("xp-filters");
  });
  test("an opened lesson shows its earlier wording and a said-in-a-task source", () => {
    const out = renderToStaticMarkup(createElement(LessonCard, { it: item({ trust: "user", via: "session", confirmed: 2,
      history: [{ claim: "Old wording here", at: 1 }] }), initialOpen: true, nowMs: 0 }));
    expect(out).toContain("Earlier wording");
    expect(out).toContain("Old wording here");
    expect(out).toContain("you said this in a task");
    expect(out).toContain("confirmed 2×");
  });
  test("worked and failed lessons: the source label, and an amber \"Does not work\" marker", () => {
    const failed = renderToStaticMarkup(createElement(LessonCard, { it: item({ kind: "failed", claim: "Loading the full file" }) }));
    expect(failed).toContain(">Does not work<");
    expect(failed).toContain("tone-warn");
    expect(failed).toContain(">did not work in a task<");
    const worked = renderToStaticMarkup(createElement(LessonCard, { it: item({ kind: "worked", indexed: false }) }));
    expect(worked).toContain(">worked in a task<");
    expect(worked).toContain(">Not used yet<");
    expect(worked).not.toContain("tone-warn");
  });
  test("empty: says what will appear", () => {
    expect(renderToStaticMarkup(createElement(MemoryPanel, { items: [] }))).toContain("No lessons yet.");
    expect(renderToStaticMarkup(createElement(MemoryPanel, { items: [] }))).toContain("tell it in the chat");
  });
});

describe("ConsultsPanel", () => {
  const now = new Date(2026, 10, 17, 15, 0).getTime(), at = (d: number, h: number) => new Date(2026, 10, d, h).getTime() / 1000;
  const consult = (p: Partial<ExpertConsult>): ExpertConsult => ({ id: "c", ts: at(17, 14), question: "How do I chain UDFs?", context: "", answer: "Call it **by name**.",
    refs: [], used: [], unsure: false, ms: 18_000, ...p });
  const one = consult({ refs: ["/x/canvas/a.py", "/x/canvas/b.py"], used: ["m1"], unsure: true, context: "The user wants a map.",
    caller: { target: "/x/canvases/Airbnb_AI_Visualization/", backend: "claude" } });
  test("collapsed card: status, source, time, chevron, question, facts; none of the answer", () => {
    const out = renderToStaticMarkup(createElement(ConsultsPanel, { consults: [one], nowMs: now }));
    expect(out).toContain('aria-expanded="false"');
    expect(out).toContain("xp-cstat tone-warn");
    expect(out).toContain(">Unsure<");
    expect(out).toContain("Airbnb_AI_Visualization · claude");
    expect(out).toContain("1 h ago");
    expect(out).toContain("xp-chev");
    expect(out).toContain("How do I chain UDFs?");
    expect(out).toContain("2 files · 1 lesson used · 18s");
    expect(out).not.toContain("by name");
    expect(out).not.toContain("The user wants a map.");
    expect(out).not.toContain(">a.py<");
    expect(out).not.toContain("xp-filters");  // a single consult: no filter row
    expect(out).toContain(">Today<");
  });
  test("answered, direct; filters and day dividers newest first", () => {
    const cs = [consult({ id: "a", ts: at(16, 9), question: "Older" }), consult({ id: "b", question: "Newer", unsure: true }), consult({ id: "d", ts: at(10, 9), question: "Oldest" })];
    const out = renderToStaticMarkup(createElement(ConsultsPanel, { consults: cs, nowMs: now }));
    expect(out).toContain(">Answered<");
    expect(out).toContain("direct");
    expect(out).toMatch(/All <span class="xp-n">3</);
    expect(out).toMatch(/Answered <span class="xp-n">2</);
    expect(out).toMatch(/Unsure <span class="xp-n">1</);
    const t = out.indexOf(">Today<"), y = out.indexOf(">Yesterday<");
    expect(t).toBeGreaterThan(-1);
    expect(y).toBeGreaterThan(t);
    expect(out.indexOf("Newer")).toBeLessThan(out.indexOf("Older"));
    expect(out.indexOf("Older")).toBeLessThan(out.indexOf("Oldest"));
  });
  test("opened card: context, the answer as markdown, file chips, lessons used by claim", () => {
    const out = renderToStaticMarkup(createElement(ConsultCard, { c: one, items: [item({ id: "m1", claim: "Prefer H3 for hex maps" })], initialOpen: true, nowMs: now }));
    expect(out).toContain('aria-expanded="true"');
    expect(out).toContain("Context from the task");
    expect(out).toContain("The user wants a map.");
    expect(out).toContain(">Answer<");
    expect(out).toContain("xp-md");
    expect(out).toContain("by name");
    expect(out).toContain(">Files<");
    expect(out).toContain(">a.py<");
    expect(out).toContain('title="/x/canvas/a.py');
    expect(out).toContain(">Lessons used<");
    expect(out).toContain("Prefer H3 for hex maps");
    expect(out).not.toContain(">m1<");
  });
  test("opened: a long context is clamped with Show all; an unknown lesson falls back to its id", () => {
    const out = renderToStaticMarkup(createElement(ConsultCard, { c: consult({ context: "1\n2\n3\n4\n5\n6", used: ["gone"] }), items: [], initialOpen: true, nowMs: now }));
    expect(out).toContain("xp-ctx clamp");
    expect(out).toContain(">Show all<");
    expect(out).toContain(">gone<");
  });
  test("empty and loading", () => {
    expect(renderToStaticMarkup(createElement(ConsultsPanel, { consults: [] }))).toContain("No consults yet.");
    expect(renderToStaticMarkup(createElement(ConsultsPanel, { consults: null }))).toContain("Loading…");
  });
});

describe("ExpertPanels: the Activity tab's loading", () => {
  const real = { activity: expertApi.activity, seen: expertApi.seen };
  const mounted: ReactTestRenderer[] = [];
  afterEach(() => {
    for (const r of mounted.splice(0)) act(() => r.unmount());
    Object.assign(expertApi, real);
  });
  type Ask = { o: { before?: string; quiet?: boolean }; ok: (r: ExpertActivityReply) => void; fail: (e: Error) => void };
  /** expertApi.activity and .seen replaced: each activity call waits to be answered by the test. */
  const stub = () => {
    const asks: Ask[] = [], seen: string[] = [];
    expertApi.activity = ((_id: string, o = {}) => new Promise<ExpertActivityReply>((ok, fail) => { asks.push({ o, ok, fail }); })) as typeof expertApi.activity;
    expertApi.seen = (async (id: string) => { seen.push(id); return {}; }) as typeof expertApi.seen;
    return { asks, seen };
  };
  const mount = async () => {
    let r!: ReactTestRenderer;
    await act(async () => { r = create(createElement(ExpertPanels, { b: { id: "x1", seq: 0 } as unknown as Bot })); });
    mounted.push(r);
    return r;
  };
  const press = (r: ReactTestRenderer, label: string) => act(async () => {
    r.root.findAll((n) => n.type === "button" && n.children.join("") === label)[0].props.onClick();
  });
  const rows = (p: string) => Array.from({ length: 100 }, (_, i): ExpertActivity => ({ id: `${p}${i}`, ts: 1000 - i, action: "learned", target: "",
    summary: `${p} line ${i}`, why: "", ok: true, undoable: false, undone: false }));
  const reply = (items: ExpertActivity[], unread = 0): ExpertActivityReply => ({ items, unread, waiting: 0 });

  test("an older page asked for under another Show everything setting is dropped", async () => {
    const { asks } = stub(), r = await mount();
    await act(async () => { asks[0].ok(reply(rows("n"))); });
    await press(r, "Show earlier");
    expect(asks[1].o.before).toBe("n99");
    await press(r, "Show everything");
    const fresh = asks.findIndex((a) => a.o.quiet);
    expect(fresh).toBeGreaterThan(1);
    // The new setting's first page ends on the same line the old request paged from.
    await act(async () => { asks[fresh].ok(reply(rows("n"))); });
    await act(async () => { asks[1].ok(reply(rows("old"))); });
    const out = JSON.stringify(r.toJSON());
    expect(out).toContain("n line 99");
    expect(out).not.toContain("old line");
  });

  test("the activity is marked seen only once it has loaded into the open tab", async () => {
    const { asks, seen } = stub(), r = await mount();
    await act(async () => { asks[0].fail(new Error("offline")); });
    expect(seen).toEqual([]);
    // The next load (here: after leaving the tab and coming back) succeeds.
    await press(r, "Memory");
    await press(r, "Activity");
    const again = asks.length - 1;
    expect(again).toBeGreaterThan(0);
    await act(async () => { asks[again].ok(reply(rows("n").slice(0, 2), 2)); });
    expect(seen).toEqual(["x1"]);
  });
});
