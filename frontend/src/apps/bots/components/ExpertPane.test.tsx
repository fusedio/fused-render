import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import type { ExpertArchived, ExpertChecks, ExpertConsult, ExpertGap, ExpertItem, ExpertObs, ExpertProposal } from "../lib/api";

const { MemoryPanel, LessonCard, ConsultsPanel, ConsultCard, ChecksPanel, ObservationsPanel, ObsCard } = await import("./ExpertPane");

const item = (p: Partial<ExpertItem>): ExpertItem => ({ id: "m", claim: "c", kind: "lesson", trust: "outcome", count: 1, sessions: [], about: [],
  created: 0, updated: 0, proposed: false, promoted: false, rejected: false, ...p });
const noop = () => {};

describe("MemoryPanel", () => {
  const now = new Date(2026, 10, 17, 15, 0).getTime();
  test("lesson cards most used first: status, source, used N×, claim, facts; no Forget while closed", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ id: "a", claim: "Alpha", count: 1, indexed: false }),
      item({ id: "b", claim: "Beta", count: 2, cited: 3, trust: "user", promoted: true }), item({ id: "c", claim: "Gamma", count: 2, contradicted: 1 })], onForget: noop }));
    expect(out.indexOf("Beta")).toBeLessThan(out.indexOf("Gamma"));
    expect(out.indexOf("Gamma")).toBeLessThan(out.indexOf("Alpha"));
    expect(out).toContain('aria-expanded="false"');
    expect(out).toContain(">In use<");
    expect(out).toContain(">Not used yet<");
    expect(out).toContain(">Contradicted<");
    expect(out).toContain("tone-warn");
    expect(out).toContain(">taught by you<");
    expect(out).toContain(">from sessions<");
    expect(out).toContain("used 5×");
    expect(out).not.toContain("seen 5×");
    expect(out).not.toContain("in a skill");
    expect(out).toContain(">contradicted 1×<");
    expect(out).toContain(">seen once<");
    expect(out).not.toContain(">Forget<");
    expect(out).toMatch(/All <span class="xp-n">3</);
    expect(out).toMatch(/In use <span class="xp-n">2</);
    expect(out).toMatch(/Not used yet <span class="xp-n">1</);
    expect(out).not.toContain("Not used in answers yet");
    expect(out).not.toContain("Consolidate");
    // An older server: no budget line, no removed section.
    expect(out).not.toContain("xp-budget");
    expect(out).not.toContain("Recently removed");
  });
  test("budget line on top: count, meter, the sentence for the state; the last tidy when recent", () => {
    const items = [item({ id: "a", claim: "Alpha" }), item({ id: "b", claim: "Beta" })];
    const below = renderToStaticMarkup(createElement(MemoryPanel, { items, onForget: noop, nowMs: now, budget: { limit: 50, tidy_at: 30, total: 12, last_tidy: null } }));
    expect(below).toContain("12 of 50 lessons");
    expect(below).toContain("Tidying starts above 30.");
    expect(below).toContain('role="meter"');
    expect(below).not.toContain("xp-meter-over");
    expect(below).not.toContain("Last tidy");
    expect(below.indexOf("12 of 50 lessons")).toBeLessThan(below.indexOf("xp-filters"));
    const above = renderToStaticMarkup(createElement(MemoryPanel, { items, onForget: noop, nowMs: now,
      budget: { limit: 50, tidy_at: 30, total: 32, last_tidy: { at: now / 1000 - 3600, deleted: [{ id: "x", claim: "X" }], promoting: [{ id: "y", claim: "Y" }, { id: "z", claim: "Z" }], over_limit: [] } } }));
    expect(above).toContain("32 of 50 lessons");
    expect(above).toContain("Above 30: each consolidation removes old, rarely used lessons and proposes heavily used ones as skills.");
    expect(above).toContain("xp-meter-over");
    expect(above).toContain("Last tidy: removed 1, proposing 2 as skills.");
    // No limit: no line at all.
    expect(renderToStaticMarkup(createElement(MemoryPanel, { items, onForget: noop, budget: { tidy_at: 30, total: 2 } }))).not.toContain("xp-budget");
  });
  test("recently removed: collapsed, counted, at most 20, the reason in words and the model's why", () => {
    const arch = (p: Partial<ExpertArchived>): ExpertArchived => ({ id: "r", claim: "Old", kind: "lesson", trust: "outcome", count: 1, sessions: [], about: [],
      created: 0, updated: 0, removed_at: now / 1000 - 7200, reason: "stale", ...p });
    const archived = [arch({ id: "a", claim: "Gone stale", why: "not used in months" }), arch({ id: "b", claim: "Now a skill", reason: "promoted", skill: "h3-maps" }),
      arch({ id: "c", reason: "over_limit" }), arch({ id: "d", reason: "forgotten" }), ...Array.from({ length: 30 }, (_, i) => arch({ id: `z${i}` }))];
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [], onForget: noop, nowMs: now, archived }));
    expect(out).toContain("<details");
    expect(out).not.toContain("<details open");
    expect(out).toContain("Recently removed (20)");
    expect(out).toContain("removed: old and rarely used · 2 h ago");
    expect(out).toContain("not used in months");
    expect(out).toContain("moved into the skill h3-maps");
    expect(out).toContain("removed: memory was over its limit");
    expect(out).toContain("forgotten by you");
    expect(out.match(/xp-removed-row/g)?.length).toBe(20);
    expect(renderToStaticMarkup(createElement(MemoryPanel, { items: [], onForget: noop, archived: [] }))).not.toContain("Recently removed");
  });
  test("open proposals above the list; a single lesson has no filter row", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ id: "a", claim: "Researched", trust: "research", indexed: false })], onForget: noop,
      proposals: [{ id: "p", ts: 1, status: "preparing", action: "create", skill: null, title: "", trigger: "", body: "", diff: "", items: [], score_before: null, score_after: null, card_seq: null }] }));
    expect(out).toContain("Drafting a skill proposal…");
    expect(out.indexOf("Drafting a skill proposal…")).toBeLessThan(out.indexOf("Researched"));
    expect(out).toContain(">found by research<");
    expect(out).toContain(">waiting for your approval<");
    expect(out).not.toContain("xp-filters");
  });
  test("opened lesson: about chips, sessions / answers, first seen / last used, Forget (not on the phone)", () => {
    const it = item({ id: "a", claim: "Use H3", about: ["maps", "h3"], count: 2, cited: 4, created: now / 1000 - 3 * 86400, updated: now / 1000 - 7200,
      last_used: now / 1000 - 600, sessions: ["s1", "s2"] });
    const out = renderToStaticMarkup(createElement(LessonCard, { it, onForget: noop, initialOpen: true, nowMs: now }));
    expect(out).toContain('aria-expanded="true"');
    expect(out).toContain(">About<");
    expect(out).toContain(">maps<");
    expect(out).toContain("Seen in 2 sessions · relied on in 4 answers");
    expect(out).toContain("first seen 3 d ago · last used 10 min ago");
    // An older server (no cited / last_used): the sessions alone and the last seen time.
    const old = renderToStaticMarkup(createElement(LessonCard, { it: item({ count: 1, updated: now / 1000 - 7200 }), onForget: noop, initialOpen: true, nowMs: now }));
    expect(old).toContain("Seen in 1 session<");
    expect(old).toContain("last seen 2 h ago");
    expect(out).toContain(">Forget<");
    expect(renderToStaticMarkup(createElement(LessonCard, { it, onForget: noop, initialOpen: true, lan: true, nowMs: now }))).not.toContain(">Forget<");
  });
  test("empty: says what will appear", () => {
    expect(renderToStaticMarkup(createElement(MemoryPanel, { items: [], onForget: noop }))).toContain("No lessons yet.");
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
    expect(out).toContain("Context from the session");
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

describe("ChecksPanel", () => {
  const checks = (p: Partial<ExpertChecks>): ExpertChecks => ({ latest: null, status: { running: false, run_id: null, done: 0, total: 0 }, questions: 2, ...p });
  test("the latest score and each question's verdict", () => {
    const out = renderToStaticMarkup(createElement(ChecksPanel, { starting: false, onRun: noop, checks: checks({ latest: { id: "r", ts: 1, score: 1, total: 2, engine: "claude",
      results: [{ q: "Q one", passed: true, why: "named it", answer: "A1" }, { q: "Q two", passed: false, why: "no example", answer: "A2" }] } }) }));
    expect(out).toContain("1 / 2");
    expect(out).toContain("Q one");
    expect(out).toContain("no example");
    expect(out).toContain("xp-mark pass");
    expect(out).toContain("xp-mark fail");
    expect(out).toContain(">Run checks<");
  });
  test("running: progress, the button waits", () => {
    const out = renderToStaticMarkup(createElement(ChecksPanel, { starting: false, onRun: noop, checks: checks({ status: { running: true, run_id: "r", done: 1, total: 2 } }) }));
    expect(out).toContain("Running checks · 1 of 2");
    expect(out).toContain("disabled");
  });
  test("no questions: the empty state", () => {
    const out = renderToStaticMarkup(createElement(ChecksPanel, { starting: false, onRun: noop, checks: checks({ questions: 0 }) }));
    expect(out).toContain("no check questions yet");
    expect(out).not.toContain("Run checks");
  });
});

describe("ObservationsPanel", () => {
  const gap = (p: Partial<ExpertGap>): ExpertGap => ({ id: "g", question: "q", questions: [], count: 1, consults: [], sessions: [], created: 0, updated: 0,
    status: "open", attempts: 0, finding: null, proposal: null, card_seq: null, resolution: "", error: "", ...p });
  const obs = (p: Partial<ExpertObs>): ExpertObs => ({ id: "o", ts: 1, kind: "user_correction", claim: "c", trust: "user", session: "", about: [], consult: "", ...p });
  const pending = [obs({ id: "p2", ts: 20, claim: "Newer waiting", kind: "repeated_request", source: { target: "/x/canvases/sales", backend: "claude" } }),
    obs({ id: "p1", ts: 10, claim: "Older waiting", kind: "gap", consult: "c1", gap: gap({ id: "g1", count: 3, error: "engine timed out",
      finding: { answer: "Found it", refs: ["/x/y/z.py"] } }) })];
  const done = [obs({ id: "d1", ts: 5, claim: "Old done", outcome: { op: "add" } }), obs({ id: "d2", ts: 9, claim: "New done", kind: "error_then_fix", outcome: { op: "bump" } }),
    obs({ id: "d4", ts: 6, claim: "Line charts for regional canvases", kind: "advice_contradicted", outcome: { op: "contradict", item: "m1", new: "m2" } }),
    obs({ id: "d5", ts: 6, claim: "Held up", kind: "advice_confirmed", outcome: { op: "bump", item: "m3" } }),
    obs({ id: "d3", ts: 7, claim: "Settled gap", kind: "gap", outcome: { op: "gap" }, gap: gap({ id: "g2", status: "answered", resolution: "user" }) })];
  const base = { pending, done, onConsolidate: noop, onDrop: noop };
  test("intro and Consolidate on top; Waiting oldest first, then Consolidated newest first; markers, sources, facts", () => {
    const out = renderToStaticMarkup(createElement(ObservationsPanel, base));
    expect(out).toContain("Everything I noted from consults and sessions.");
    expect(out.split(">Consolidate now<").length - 1).toBe(1);
    const w = out.indexOf(">Waiting<"), c = out.indexOf(">Consolidated<");
    expect(w).toBeGreaterThan(-1);
    expect(c).toBeGreaterThan(w);
    expect(out.indexOf("Older waiting")).toBeLessThan(out.indexOf("Newer waiting"));
    expect(out.indexOf("Newer waiting")).toBeLessThan(c);
    expect(out.indexOf("New done")).toBeLessThan(out.indexOf("Settled gap"));
    expect(out.indexOf("Settled gap")).toBeLessThan(out.indexOf("Old done"));
    expect(out).toMatch(/xp-cstat tone-warn"><span class="xp-dot" aria-hidden="true"><\/span>unsure</);
    expect(out).toContain(">asked for<");
    expect(out).toContain(">fix that worked<");
    expect(out).toContain(">waiting to be consolidated<");
    expect(out).toContain(">new lesson<");
    expect(out).toContain(">counted toward a lesson<");
    expect(out).toContain(">answered by you<");
    expect(out).toMatch(/tone-warn"><span class="xp-dot" aria-hidden="true"><\/span>advice contradicted</);
    expect(out).toContain(">contradicted a lesson<");
    expect(out).toContain(">advice confirmed<");
    expect(out).toContain(">sales · claude<");
    expect(out).toContain(">consult<");
    expect(out).toContain("asked 3×");
    expect(out).not.toContain("Found it");  // a finding only when opened
    expect(out).not.toContain(">Drop<");
    expect(out).toMatch(/All <span class="xp-n">7</);
    expect(out).toMatch(/Unsure <span class="xp-n">2</);
    expect(out).toMatch(/Learnings <span class="xp-n">5</);
    expect(out).toContain("xp-day");
  });
  test("opened: the lesson it landed on, both sides of a contradiction, a gap's note, finding, files and Drop", () => {
    const items = [item({ id: "m1", claim: "Old rule" }), item({ id: "m2", claim: "New rule" })];
    const contra = renderToStaticMarkup(createElement(ObsCard, { o: done[2], consolidated: true, researching: null, proposals: null, items, lan: false, onDrop: noop, initialOpen: true }));
    expect(contra).toContain(">Contradicted<");
    expect(contra).toContain("Old rule");
    expect(contra).toContain(">Corrected rule<");
    expect(contra).toContain("New rule");
    const bump = renderToStaticMarkup(createElement(ObsCard, { o: obs({ outcome: { op: "bump", item: "m2" } }), consolidated: true, researching: null, proposals: null, items, lan: false, onDrop: noop, initialOpen: true }));
    expect(bump).toContain(">Lesson<");
    expect(bump).toContain("New rule");
    const g = obs({ kind: "gap", claim: "Q", gap: gap({ error: "not found: nothing on it", finding: { answer: "Found it", refs: ["/x/y/z.py"] } }) });
    const unsure = renderToStaticMarkup(createElement(ObsCard, { o: g, consolidated: false, researching: null, proposals: null, items, lan: false, onDrop: noop, initialOpen: true }));
    expect(unsure).toContain("Looked into it: nothing on it");
    expect(unsure).toContain("Found it");
    expect(unsure).toContain(">z.py<");
    expect(unsure).toContain(">Drop<");
    expect(renderToStaticMarkup(createElement(ObsCard, { o: g, consolidated: false, researching: null, proposals: null, items, lan: true, onDrop: noop, initialOpen: true }))).not.toContain(">Drop<");
  });
  test("while consolidating: the stage line, the button waits, the researched gap says so", () => {
    const out = renderToStaticMarkup(createElement(ObservationsPanel, { ...base, consolidating: { running: true, stage: "researching", gap: "g1" } }));
    expect(out).toContain("Researching an open question…");
    expect(out).toContain(">Consolidating…<");
    expect(out).toContain(">researching… · asked 3×<");
  });
  test("nothing waiting and no open question: the button is off", () => {
    const out = renderToStaticMarkup(createElement(ObservationsPanel, { ...base, pending: [], done: [done[0]] }));
    expect(out).toMatch(/disabled=""[^>]*>Consolidate now</);
  });
  test("proposals: status lines under the intro, a proposed gap's wording, its failure muted; text and note cleaned", () => {
    const prop = (id: string, status: ExpertProposal["status"], error = ""): ExpertProposal => ({ id, ts: 1, status, error, action: "create", skill: null, title: "", trigger: "", body: "",
      diff: "", items: [], score_before: null, score_after: null, card_seq: null });
    const pend = [obs({ id: "u", kind: "gap", claim: "Could not answer: Which basemap?", gap: gap({ id: "g5", status: "proposed", proposal: "p1", error: "not found: no doc on it" }) }),
      obs({ id: "v", kind: "gap", claim: "Other", gap: gap({ id: "g6", status: "proposed", proposal_status: "failed", proposal_error: "diff did not apply" }) })];
    const out = renderToStaticMarkup(createElement(ObservationsPanel, { ...base, pending: pend, done: [], proposals: [prop("p1", "preparing")] }));
    expect(out).toContain("Drafting a skill proposal…");
    expect(out.indexOf("Drafting a skill proposal…")).toBeGreaterThan(out.indexOf("Everything I noted"));
    expect(out).toContain(">drafting a skill fix…<");
    expect(out).toContain(">skill fix failed<");
    expect(out).toContain("diff did not apply");
    expect(out).toContain(">Which basemap?<");
    expect(out).not.toContain("Could not answer:");
    expect(out).not.toContain("no doc on it");  // the research note shows only when the card is opened
    expect(out).not.toContain("proposed in chat");
  });
  test("phone: no Consolidate; empty state", () => {
    const out = renderToStaticMarkup(createElement(ObservationsPanel, { ...base, lan: true }));
    expect(out).not.toContain("Consolidate now");
    expect(renderToStaticMarkup(createElement(ObservationsPanel, { ...base, pending: [], done: [] }))).toContain("Nothing noted yet.");
  });
});
