// THE ONE MARKDOWN FUNNEL, and the two things about it the user reported (#14
// "markdown tables not rendered", #16 "HTML in assistant text sometimes renders
// as a code block, sometimes as text — inconsistent across the cards wall, Peek
// and chat"). Both are properties of the funnel alone, which is why they are
// tested here rather than through three mounted hosts.
//
// THE ASSERTIONS RUN AGAINST THE `marked` STAGE, deliberately, and reach it
// through the shared `marked` singleton rather than through a second export:
// `marked.use()` mutates that singleton, so one `renderMd` call installs this
// module's config globally and `marked.parse` here is then the exact parser the
// chat uses. The DOMPurify stage cannot run in bun (no DOM — `renderMd` takes
// its documented `<pre>` fallback), and it is not what either report is about:
// sanitizing is a security boundary, and these two are about what the reader
// sees. `renderMd`'s own fallback is covered at the bottom.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { marked } from "marked";

import { renderMd, TABLE_WRAP_CLASS } from "./markdown";

/** Install this module's `marked.use()` config on the shared singleton. */
renderMd("configure me");
const md = (text: string) => marked.parse(text, { async: false }) as string;

test("a GFM table parses into a real table (#14)", () => {
  const html = md("| file | lines |\n| --- | --- |\n| a.ts | 12 |\n");
  expect(html).toContain("<table>");
  expect(html).toContain("<th>file</th>");
  expect(html).toContain("<td>a.ts</td>");
});

test("`breaks: true` does not break a table: a lone newline is still a <br> (#14)", () => {
  // The pairing worth pinning: GFM tables are a BLOCK rule and single-newline
  // line breaks are an INLINE one, and turning the second on must not eat the
  // first. This is the config that shipped; it is the CSS that was missing.
  expect(md("| a |\n| --- |\n| 1 |\n")).toContain("<table>");
  expect(md("one\ntwo\n")).toContain("<br>");
});

test("the table scroller's class is the one the stylesheet paints", () => {
  const css = readFileSync(
    join(dirname(new URL(import.meta.url).pathname), "../styles/transcript.css"),
    "utf8",
  );
  expect(css).toContain("." + TABLE_WRAP_CLASS);
});

// ── the raw-HTML rule (#16) ────────────────────────────────────────────────
// Model-authored markup is SHOWN, never rendered. Before this it came out three
// different ways depending on how the model happened to indent it, so one reply
// looked like three different messages across the three hosts.

test("a raw block tag comes out as text, not as an element", () => {
  const html = md("here it is: <div>hi</div>\n");
  expect(html).toContain("&lt;div&gt;hi&lt;/div&gt;");
  expect(html).not.toContain("<div>");
});

test("a raw <img> is text too — it never becomes a request or an onerror", () => {
  const html = md('an <img src="x.png" onerror="boom()"> tag\n');
  expect(html).not.toContain("<img");
  expect(html).toContain("&lt;img");
});

test("the fenced, indented and inline cases now AGREE — which is the report", () => {
  for (const source of ["<div>hi</div>\n", "```\n<div>hi</div>\n```\n", "    <div>hi</div>\n"]) {
    const html = md(source);
    expect(html).toContain("&lt;div&gt;hi&lt;/div&gt;");
    expect(html).not.toContain("<div>");
  }
});

test("markdown's OWN markup is untouched: the rule is about the model's input", () => {
  const html = md("**bold** and `code` and [a](https://x.test)\n");
  expect(html).toContain("<strong>bold</strong>");
  expect(html).toContain("<code>code</code>");
  expect(html).toContain('href="https://x.test"');
  // The link renderer's own attributes, which the sanitizer's ADD_ATTR keeps.
  expect(html).toContain('rel="noopener noreferrer"');
});

test("headings, nested lists and a blockquote come out as real elements (#15)", () => {
  const html = md("# One\n\n- a\n  - b\n\n> quoted\n\n---\n");
  expect(html).toContain("<h1>One</h1>");
  // A nested list is a `<ul>` INSIDE an `<li>` — the shape the CSS indents.
  expect(html).toMatch(/<li>a[\s\S]*<ul>[\s\S]*<li>b<\/li>/);
  expect(html).toContain("<blockquote>");
  expect(html).toContain("<hr>");
});

test("an unclosed fence mid-stream is closed rather than swallowing the tail", () => {
  // Through `renderMd`, since the fence repair is its own step. With no DOM the
  // sanitizer stage falls back to a `<pre>` of escaped text, and the point here
  // is only that the tail is still IN it rather than lost.
  expect(renderMd("before\n```js\nconst a = 1;")).toContain("const a = 1;");
});
