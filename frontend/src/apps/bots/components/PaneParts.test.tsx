import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const { DiffView } = await import("./PaneParts");
const { MdDoc, unesc } = await import("./MdDoc");

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
