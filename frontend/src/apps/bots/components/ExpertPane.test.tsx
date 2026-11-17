import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import type { ExpertChecks, ExpertConsult, ExpertGap, ExpertItem, ExpertObs, ExpertProposal } from "../lib/api";

const { MemoryPanel, ConsultsPanel, ConsultCard, ChecksPanel, ObservationsPanel } = await import("./ExpertPane");

const item = (p: Partial<ExpertItem>): ExpertItem => ({ id: "m", claim: "c", kind: "lesson", trust: "outcome", count: 1, sessions: [], about: [],
  created: 0, updated: 0, proposed: false, promoted: false, rejected: false, ...p });
const noop = () => {};

describe("MemoryPanel", () => {
  test("lessons most seen first, with their tags and a Forget each; no observations or Consolidate here", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ id: "a", claim: "Alpha", count: 1 }), item({ id: "b", claim: "Beta", count: 5, trust: "user", promoted: true })],
      onForget: noop }));
    expect(out.indexOf("Beta")).toBeLessThan(out.indexOf("Alpha"));
    expect(out).toContain("seen 5×");
    expect(out).toContain('xp-tag user">user<');
    expect(out).toContain('xp-tag promoted">promoted<');
    expect(out.split(">Forget<").length - 1).toBe(2);
    expect(out).not.toContain("Consolidate");
    expect(out).not.toContain("observation");
  });
  test("not-yet-used lessons under their divider with the reason, open proposals above", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ id: "a", claim: "Indexed one", count: 2 }), item({ id: "b", claim: "Once only", indexed: false }),
      item({ id: "c", claim: "Researched", trust: "research", indexed: false })], onForget: noop,
      proposals: [{ id: "p", ts: 1, status: "preparing", action: "create", skill: null, title: "", trigger: "", body: "", diff: "", items: [], score_before: null, score_after: null, card_seq: null }] }));
    expect(out).toContain("Drafting a skill proposal…");
    const div = out.indexOf("Not used in answers yet");
    expect(div).toBeGreaterThan(out.indexOf("Indexed one"));
    expect(out.indexOf("Once only")).toBeGreaterThan(div);
    expect(out).toContain('xp-tag seen-once">seen once<');
    expect(out).toContain(">found by research<");
    expect(out).toContain(">waiting for your approval<");
  });
  test("a contradicted lesson carries its warning tag", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ id: "a", claim: "Bar charts only", count: 2, contradicted: 1, contradicted_sessions: ["s"] })], onForget: noop }));
    expect(out).toContain('xp-tag contradicted">contradicted 1×<');
  });
  test("no divider when every lesson is indexed", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ id: "a" })], onForget: noop }));
    expect(out).not.toContain("Not used in answers yet");
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
    expect(out).toContain('xp-cstat unsure');
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
  test("intro and Consolidate on top; Waiting oldest first, then Consolidated newest first, with tags and results", () => {
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
    expect(out).toContain(">unsure<");
    expect(out).toContain(">asked for<");
    expect(out).toContain(">fix that worked<");
    expect(out).toContain(">new lesson<");
    expect(out).toContain(">counted toward a lesson<");
    expect(out).toContain(">answered by you<");
    expect(out).toContain('xp-tag kind-advice_contradicted">advice contradicted<');
    expect(out).toContain('xp-tag res-contradict">contradicted a lesson<');
    expect(out).toContain(">advice confirmed<");
    expect(out).toContain("sales · claude");
    expect(out).toContain(" · consult<");
    expect(out).toContain("asked 3×");
    expect(out).toContain("engine timed out");
    expect(out).toContain("<details>");
    expect(out).toContain(">z.py<");
    expect(out.split(">Drop<").length - 1).toBe(1);
  });
  test("while consolidating: the stage line, the button waits, the researched gap says so", () => {
    const out = renderToStaticMarkup(createElement(ObservationsPanel, { ...base, consolidating: { running: true, stage: "researching", gap: "g1" } }));
    expect(out).toContain("Researching an open question…");
    expect(out).toContain(">Consolidating…<");
    expect(out).toContain("researching…");
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
    expect(out).toContain("Looked into it: no doc on it");
    expect(out).not.toContain("proposed in chat");
  });
  test("phone: no Consolidate, no Drop; empty state", () => {
    const out = renderToStaticMarkup(createElement(ObservationsPanel, { ...base, lan: true }));
    expect(out).not.toContain(">Drop<");
    expect(out).not.toContain("Consolidate now");
    expect(renderToStaticMarkup(createElement(ObservationsPanel, { ...base, pending: [], done: [] }))).toContain("Nothing noted yet.");
  });
});
