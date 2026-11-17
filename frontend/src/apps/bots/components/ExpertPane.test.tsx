import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import type { ExpertChecks, ExpertGap, ExpertItem, ExpertObs } from "../lib/api";

const { MemoryPanel, ConsultsPanel, ChecksPanel, ObservationsPanel } = await import("./ExpertPane");

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
    expect(out).toContain("Drafting a skill proposal and running the checks…");
    const div = out.indexOf("Not used in answers yet");
    expect(div).toBeGreaterThan(out.indexOf("Indexed one"));
    expect(out.indexOf("Once only")).toBeGreaterThan(div);
    expect(out).toContain('xp-tag seen-once">seen once<');
    expect(out).toContain(">found by research<");
    expect(out).toContain(">waiting for your approval<");
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
  test("question, unsure tag, answer and refs behind the fold", () => {
    const out = renderToStaticMarkup(createElement(ConsultsPanel, { consults: [{ id: "c", ts: 1, question: "How do I chain UDFs?", context: "", answer: "Call it by name.",
      refs: ["/x/canvas/a.py"], used: [], unsure: true, ms: 10, caller: { target: "/x/canvas/sales", backend: "claude" } }] }));
    expect(out).toContain("How do I chain UDFs?");
    expect(out).toContain("sales · claude");
    expect(out).toContain(">unsure<");
    expect(out).toContain("<details>");
    expect(out).toContain("Call it by name.");
    expect(out).toContain(">a.py<");
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
  test("phone: no Consolidate, no Drop; empty state", () => {
    const out = renderToStaticMarkup(createElement(ObservationsPanel, { ...base, lan: true }));
    expect(out).not.toContain(">Drop<");
    expect(out).not.toContain("Consolidate now");
    expect(renderToStaticMarkup(createElement(ObservationsPanel, { ...base, pending: [], done: [] }))).toContain("Nothing noted yet.");
  });
});
