import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import type { ExpertMemory, ExpertSession } from "../lib/api";

const { DiffView, MemoryPanel, SessionsPanel, Transcript } = await import("./ExpertPane");
const { MdDoc, unesc } = await import("./MdDoc");
const { menuItems } = await import("./menuIcons");

const now = Date.UTC(2026, 10, 17, 15, 0);
const at = (minAgo: number) => now / 1000 - minAgo * 60;
const DIFF = "--- a/memory.md\n+++ b/memory.md\n@@ -1 +1,2 @@\n ## Works\n+- [1 · 2026-11-17] Pull first.\n";
const row = (p: Partial<ExpertSession>): ExpertSession => ({ id: "s", kind: "ask", started: at(5), ended: at(4), status: "done", memory_diff: "", ...p });
const noop = () => {};

describe("MemoryPanel", () => {
  const mem: ExpertMemory = { text: "## Works\n- [1 · 2026-11-17] Pull <first>.\n", path: "/x/Canvas Reference/memory.md", lines: 2, bytes: 40,
    limit_lines: 200, limit_bytes: 12288, edited_by_hand: true,
    changes: [{ id: "c1", kind: "task-learn", started: at(30), trigger: { target: "/w/maps", backend: "claude" } },
      { id: "c2", kind: "chat", started: at(90), trigger: { message: "Always pull first" } }] };
  test("the file read-only and rendered as markdown, its path, size, the hand-edit note, then who changed it", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { memory: mem, onChange: noop, nowMs: now }));
    expect(out).toContain('<div class="xp-mem xp-md" aria-label="memory.md"><h2>Works</h2><ul><li>[1 · 2026-11-17] Pull &lt;first&gt;.</li></ul></div>');
    expect(out).not.toContain("<pre");
    expect(out).not.toMatch(/<textarea|contenteditable/);
    expect(out).toContain('>/x/Canvas Reference/memory.md <a class="xp-edit" href="/explorer/view/x/Canvas%20Reference/memory.md"');
    expect(out).toContain("2 of 200 lines · 40 B of 12 KB");
    expect(out).toContain("Edited by hand since the last session.");
    expect(out).toContain(">task in maps (claude)<");
    expect(out).toContain(">“Always pull first”<");
    expect(out).toContain(">30m ago<");
    expect(out.indexOf("task in maps")).toBeLessThan(out.indexOf("Always pull first"));
  });
  test("no hand note when not edited; empty file and no changes say so", () => {
    const out = renderToStaticMarkup(createElement(MemoryPanel, { memory: { ...mem, text: "", edited_by_hand: false, changes: [] }, onChange: noop }));
    expect(out).not.toContain("Edited by hand");
    expect(out).toContain("Nothing remembered yet.");
    expect(out).toContain("No session has changed it yet.");
  });
});

describe("SessionsPanel", () => {
  const rows = [row({ id: "old", started: at(60), kind: "dream", trigger: { lines: 230 }, memory_diff: DIFF }),
    row({ id: "new", started: at(2), ended: null, status: "running", trigger: { asker: "a task", question: "How?" } }),
    row({ id: "mid", started: at(20), kind: "task-learn", status: "error", error: "harness exited", trigger: { target: "/w/maps" }, memory_diff: "" })];
  const base = { rows, open: null, onOpen: noop, full: null, nowMs: now };
  test("newest first, with kind, status, origin, and the changed-memory tag only where memory changed; no filter, no +/−", () => {
    const out = renderToStaticMarkup(createElement(SessionsPanel, base));
    expect(out.indexOf('id="xps-new"')).toBeLessThan(out.indexOf('id="xps-mid"'));
    expect(out.indexOf('id="xps-mid"')).toBeLessThan(out.indexOf('id="xps-old"'));
    expect(out).toContain(">running<");
    expect(out).toContain(">error<");
    expect(out).toContain(">a task: How?<");
    expect(out).toContain(">memory at 230 lines<");
    expect(out).toContain('>dream<');
    expect(out.match(/class="xp-chg"[^>]*>changed memory</g)?.length).toBe(1);
    expect(out).not.toContain("+1 −0");
    expect(out).not.toMatch(/xp-filters|xp-chip|>All</);
  });
  test("an open row shows its error, then its diff, then the prompt and the rest of the transcript", () => {
    const full = { ...rows[2], memory_diff: DIFF, transcript: [{ role: "prompt", text: "Update your memory" }, { role: "tool", text: "Edit memory.md" }, { role: "refused", text: "Write skills/a.md" }] };
    const out = renderToStaticMarkup(createElement(SessionsPanel, { ...base, open: "mid", full }));
    expect(out).toContain('aria-expanded="true"');
    expect(out).toContain("harness exited");
    expect(out).toContain('class="xp-dr add"');
    expect(out).toContain('class="xp-turn role-refused"');
    expect(out).toContain(">prompt<");
    expect(out.indexOf("harness exited")).toBeLessThan(out.indexOf("Memory diff"));
    expect(out.indexOf("Memory diff")).toBeLessThan(out.indexOf("Update your memory"));
  });
  test("a session opened from Memory that the list lacks shows on top", () => {
    const full = row({ id: "gone", kind: "chat", trigger: { message: "hi" }, memory_diff: DIFF });
    const out = renderToStaticMarkup(createElement(SessionsPanel, { ...base, open: "gone", full }));
    expect(out).toContain(">“hi”<");
    expect(out).toContain('class="xp-dr add"');
    expect(out).not.toContain(">Transcript<");  // a chat row has none
  });
  test("a session opened from Memory that neither the list nor a fetch holds yet says loading, then its error", () => {
    const loading = renderToStaticMarkup(createElement(SessionsPanel, { ...base, open: "gone" }));
    expect(loading).toContain('id="xps-gone"');
    expect(loading).toContain("Loading this session…");
    const failed = renderToStaticMarkup(createElement(SessionsPanel, { ...base, open: "gone", fullErr: "expert session: 404" }));
    expect(failed).toContain('role="alert">expert session: 404<');
  });
});

describe("DiffView", () => {
  const md = (text: string) => renderToStaticMarkup(createElement(MdDoc, { text }));
  test("no file or hunk headers; added and removed lines on their own rows with a gutter mark", () => {
    const out = renderToStaticMarkup(createElement(DiffView, { diff: "--- a/memory.md\n+++ b/memory.md\n@@ -1,3 +1,3 @@\n ## Works\n-- gone\n-- also gone\n+- new\n" }));
    expect(out).not.toMatch(/---|\+\+\+|@@/);
    expect(out).toContain('<div class="xp-dr ctx"><span class="xp-g" aria-hidden="true"></span><div class="xp-dc xp-md"><h2>Works</h2></div></div>');
    expect(out).toContain('<div class="xp-dr del"><span class="xp-g" aria-hidden="true">−</span><span class="xp-sr">Removed: </span><div class="xp-dc xp-md"><ul><li>gone</li></ul></div></div>');
    expect(out).toContain('<div class="xp-dr add"><span class="xp-g" aria-hidden="true">+</span><span class="xp-sr">Added: </span><div class="xp-dc xp-md"><ul><li>new</li></ul></div></div>');
    expect(out).not.toContain("xp-dpair");
  });
  test("a count bump is one pair: the removed line, then the added one", () => {
    const out = renderToStaticMarkup(createElement(DiffView, { diff: "@@ -1 +1 @@\n-- [1 · 2026-11-16] Pull `main` first.\n+- [2 · 2026-11-17] Pull `main` first.\n" }));
    expect(out).toMatch(/^<div class="xp-diff" role="group" aria-label="Memory diff"><div class="xp-dpair"><div class="xp-dr del">.*\[1 · 2026-11-16\].*<div class="xp-dr add">.*\[2 · 2026-11-17\] Pull <code>main<\/code> first\.<\/li>/);
  });
  test("HTML in memory text is text, in the diff and in the file", () => {
    const out = renderToStaticMarkup(createElement(DiffView, { diff: "@@ -0,0 +1 @@\n+- Use <script>alert(1)</script> & `<b>x</b>` <img src=x onerror=alert(1)>\n" }));
    expect(out).not.toMatch(/<script|<b>|<img/);
    expect(out).toContain("&lt;script&gt;alert(1)&lt;/script&gt; &amp; <code>&lt;b&gt;x&lt;/b&gt;</code> &lt;img src=x onerror=alert(1)&gt;");
    expect(md("<div onclick=x>hi</div>")).toBe("<p>&lt;div onclick=x&gt;hi&lt;/div&gt;</p>");
    expect(md("[x](javascript:alert(1))")).not.toContain("href");
  });
  test("an empty diff says no change", () => {
    expect(renderToStaticMarkup(createElement(DiffView, { diff: "" }))).toContain("No change to memory.");
    expect(renderToStaticMarkup(createElement(Transcript, { turns: [] }))).toContain("No transcript.");
  });
});

describe("MdDoc", () => {
  const md = (text: string) => renderToStaticMarkup(createElement(MdDoc, { text }));
  test("the constructs memory.md uses", () => {
    expect(md("## Does not work")).toBe("<h2>Does not work</h2>");
    expect(md("- [3 · 2026-11-17] Run `fused push` with **--force** and *care*")).toBe(
      "<ul><li>[3 · 2026-11-17] Run <code>fused push</code> with <strong>--force</strong> and <em>care</em></li></ul>");
    expect(md("See [the docs](https://x.io/a) and ~~old~~")).toBe('<p>See <span class="md-link" title="https://x.io/a">the docs</span> and <del>old</del></p>');
    expect(md("1. a\n2. b")).toBe("<ol><li>a</li><li>b</li></ol>");
    expect(md("> q")).toBe("<blockquote><p>q</p></blockquote>");
    expect(md("```\n<x> & y\n```")).toBe("<pre><code>&lt;x&gt; &amp; y</code></pre>");
    expect(md("a \\* b")).toBe("<p>a * b</p>");
    expect(md("")).toBe("");
  });
  test("unesc undoes marked's escaping once", () => {
    expect(unesc("&lt;a&gt; &amp;lt; &quot;q&quot; &#39;s&#39;")).toBe("<a> &lt; \"q\" 's'");
  });
});

describe("the bot menu for an expert", () => {
  test("no live view, clone or routines; Skills… opens its skills; it exports", () => {
    const b = { id: "x", name: "X", status: "idle", seq: 0, kind: "expert" } as never;
    const items = menuItems(b, { live: true });
    const as = items.map((m) => (m === "hr" ? m : m.a));
    expect(items).toContainEqual(expect.objectContaining({ a: "exportFile", label: "Export expert…" }));
    expect(as).not.toContain("live");
    expect(as).not.toContain("clone");
    expect(as).toContain("skills");
    expect(as).not.toContain("routines");
    expect(as).toContain("settings");
  });
});
