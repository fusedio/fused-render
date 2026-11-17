import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import type { ExpertChecks, ExpertGap, ExpertItem } from "../lib/api";

const { MemoryPanel, ConsultsPanel, ChecksPanel, GapsSection } = await import("./ExpertPane");

const item = (p: Partial<ExpertItem>): ExpertItem => ({ id: "m", claim: "c", kind: "lesson", trust: "outcome", count: 1, sessions: [], about: [],
  created: 0, updated: 0, proposed: false, promoted: false, rejected: false, ...p });
const noop = () => {};

describe("MemoryPanel", () => {
  test("lessons most seen first, with their tags, a Forget each, and the pending line", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ id: "a", claim: "Alpha", count: 1 }), item({ id: "b", claim: "Beta", count: 5, trust: "user", promoted: true })],
      pending: [{ id: "o", ts: 1, kind: "user_correction", claim: "x", trust: "user", session: "", about: [], consult: "" }], due: false, busy: false, onForget: noop, onConsolidate: noop }));
    expect(out.indexOf("Beta")).toBeLessThan(out.indexOf("Alpha"));
    expect(out).toContain("seen 5×");
    expect(out).toContain('xp-tag user">user<');
    expect(out).toContain('xp-tag promoted">promoted<');
    expect(out.split(">Forget<").length - 1).toBe(2);
    expect(out).toContain("1 observation waiting to be consolidated.");
    expect(out).toContain(">Consolidate now<");
    expect(out.split(">Consolidate now<").length - 1).toBe(1);
  });
  test("seen-once lessons under their divider, open proposals above", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ id: "a", claim: "Indexed one", count: 2 }), item({ id: "b", claim: "Once only", indexed: false })],
      pending: [], due: false, busy: false, onForget: noop, onConsolidate: noop,
      proposals: [{ id: "p", ts: 1, status: "preparing", action: "create", skill: null, title: "", trigger: "", body: "", diff: "", items: [], score_before: null, score_after: null, card_seq: null }] }));
    expect(out).toContain("Drafting a skill proposal and running the checks…");
    const div = out.indexOf("Not used in answers yet");
    expect(div).toBeGreaterThan(out.indexOf("Indexed one"));
    expect(out.indexOf("Once only")).toBeGreaterThan(div);
    expect(out).toContain('xp-tag seen-once">seen once<');
  });
  test("no divider when every lesson is indexed", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [item({ id: "a" })], pending: [], due: false, busy: false, onForget: noop, onConsolidate: noop }));
    expect(out).not.toContain("Not used in answers yet");
  });
  test("empty: says what will appear", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { items: [], pending: [], due: false, busy: false, onForget: noop, onConsolidate: noop }));
    expect(out).toContain("No lessons yet.");
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

describe("Memory panel: open questions", () => {
  const gap = (p: Partial<ExpertGap>): ExpertGap => ({ id: "g", question: "q", questions: [], count: 1, consults: [], sessions: [], created: 0, updated: 0,
    status: "open", attempts: 0, finding: null, proposal: null, card_seq: null, resolution: "", error: "", ...p });
  const gaps = [gap({ id: "a", question: "Rare one", count: 1, error: "engine timed out" }),
    gap({ id: "b", question: "Common one", count: 4, finding: { answer: "Found it", refs: ["/x/y/z.py"] } }),
    gap({ id: "c", question: "Done one", status: "answered", resolution: "user" })];
  const base = { items: [item({ id: "l", claim: "A lesson", count: 2 })], pending: [], due: false, busy: false, onForget: noop, onConsolidate: noop };
  test("three sections in order; one Consolidate button; the stage line while running; resolved folded away", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { ...base, gaps, researching: "b", consolidating: { running: true, stage: "researching", gap: "b" } }));
    const w = out.indexOf("Waiting to be consolidated"), q = out.indexOf(">Open questions<"), l = out.indexOf(">Lessons<");
    expect(w).toBeGreaterThan(-1);
    expect(q).toBeGreaterThan(w);
    expect(l).toBeGreaterThan(q);
    expect(out).toContain("Consolidating looks into each one");
    expect(out).toContain("Researching an open question…");
    expect(out.split("Consolidat").length).toBeGreaterThan(1);
    expect(out.split("<button").length - 1).toBe(1 + 2 + 1 + 1); // consolidate, 2× Drop, Show resolved, Forget
    expect(out).toContain(">Consolidating…<");
    expect(out.indexOf("Common one")).toBeLessThan(out.indexOf("Rare one"));
    expect(out).toContain("researching…");
    expect(out).toContain("engine timed out");
    expect(out).toContain(">z.py<");
    expect(out).toContain("Show 1 resolved");
    expect(out).not.toContain("Done one");
    expect(out).not.toContain("Research now");
  });
  test("resolved ones, once shown, are dimmed under their divider", () => {
    const out = renderToStaticMarkup(createElement(GapsSection, { gaps, researching: null, onDrop: noop, showResolved: true }));
    expect(out.indexOf("Done one")).toBeGreaterThan(out.indexOf(">Resolved<"));
    expect(out).toContain("xp-list xp-resolved");
    expect(out).toContain("answered by you");
    expect(out).toContain("Hide resolved");
  });
  test("no gaps: no section; phone: no Consolidate, no Drop", () => {
    expect(renderToStaticMarkup(createElement(MemoryPanel, { ...base, gaps: [] }))).not.toContain("Open questions");
    const out = renderToStaticMarkup(createElement(MemoryPanel, { ...base, gaps, lan: true }));
    expect(out).not.toContain(">Drop<");
    expect(out).not.toContain("Consolidate now");
  });
});
