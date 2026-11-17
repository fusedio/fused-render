import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import type { ExpertChecks, ExpertGap, ExpertItem } from "../lib/api";

const { MemoryPanel, ConsultsPanel, ChecksPanel, GapsPanel } = await import("./ExpertPane");

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

describe("GapsPanel", () => {
  const gap = (p: Partial<ExpertGap>): ExpertGap => ({ id: "g", question: "q", questions: [], count: 1, consults: [], sessions: [], created: 0, updated: 0,
    status: "open", attempts: 0, finding: null, proposal: null, card_seq: null, resolution: "", error: "", ...p });
  test("unresolved first, resolved under the divider; the researched one says so; Drop per open row; consolidate waits while running", () => {
    const out = renderToStaticMarkup(createElement(GapsPanel, { researching: "b", onDrop: noop, onConsolidate: noop,
      consolidating: { running: true, stage: "researching", gap: "b" },
      gaps: [gap({ id: "a", question: "Rare one", count: 1 }), gap({ id: "b", question: "Common one", count: 4, finding: { answer: "Found it", refs: ["/x/y/z.py"] } }),
        gap({ id: "c", question: "Done one", status: "answered", resolution: "user", error: "" })] }));
    expect(out).toContain("When I consolidate what I have learned");
    expect(out).toContain("Researching an open question…");
    expect(out.indexOf("Common one")).toBeLessThan(out.indexOf("Rare one"));
    expect(out.indexOf("Resolved")).toBeGreaterThan(out.indexOf("Rare one"));
    expect(out).toContain("researching…");
    expect(out).toContain("answered by you");
    expect(out).toContain(">z.py<");
    expect(out.split(">Drop<").length - 1).toBe(2);
    expect(out).toContain(">Consolidating…<");
    expect(out).not.toContain("Research now");
  });
  test("phone: no buttons; empty state", () => {
    const out = renderToStaticMarkup(createElement(GapsPanel, { researching: null, onDrop: noop, onConsolidate: noop, lan: true, gaps: [gap({})] }));
    expect(out).not.toContain(">Drop<");
    expect(out).not.toContain("Consolidate now");
    const empty = renderToStaticMarkup(createElement(GapsPanel, { researching: null, onDrop: noop, onConsolidate: noop, gaps: [] }));
    expect(empty).toContain("No gaps yet.");
  });
});
