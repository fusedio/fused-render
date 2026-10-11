// Assertions run against the `marked` stage through the shared singleton (one md() call installs this module's
// marked.use config), the same way apps/claude/protocol/markdown.test.ts does: DOMPurify cannot run in bun (no DOM,
// so md() takes its documented `<pre>` fallback there), and what these cover is what the reader sees — a table is a
// table, the bot's raw HTML is text, links open away — not the sanitizer.
import { describe, expect, test } from "bun:test";
import { marked } from "marked";

import { md } from "./md";

md("configure me");
const parse = (text: string) => marked.parse(text, { async: false }) as string;

describe("md", () => {
  test("empty and nullish input", () => {
    expect(md("")).toBe("");
    expect(md("  \n")).toBe("");
    expect(md(null)).toBe("");
    expect(md(undefined)).toBe("");
  });
  test("a GFM pipe table is a real table (the Restic vs Cryptomator report)", () => {
    const html = parse("| | A: Restic | B: Cryptomator |\n|---|---|---|\n| What it is | A backup tool | A vault |\n| S3 | Built in | Not built in |\n");
    expect(html).toContain('<div class="tablewrap"><table>');  // the wrap is the sideways scroller (Bugbot, #1556)
    expect(html).toContain("<th>A: Restic</th>");
    expect(html).toContain("<td>What it is</td>");
    expect(html).toContain("<td>Not built in</td>");
    expect(html).not.toContain("|");
  });
  test("raw HTML the bot writes is text, never markup", () => {
    expect(parse("<script>alert(1)</script>")).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(parse("a <b>bold</b> b")).toContain("&lt;b&gt;bold&lt;/b&gt;");
    expect(parse("<img src=x onerror=alert(1)>")).not.toContain("<img");
  });
  test("links open in a new tab; bare URLs link; code spans keep theirs", () => {
    expect(parse("[site](https://x.io/a)")).toContain('<a href="https://x.io/a" target="_blank" rel="noopener">site</a>');
    expect(parse("see https://x.io/b")).toContain('href="https://x.io/b"');
    expect(parse("`https://x.io/a`")).toContain("<code>https://x.io/a</code>");
  });
  test("• bullets become - bullets before parsing; a line break inside a paragraph is a <br>", () => {
    expect(md("• a\n  • b")).toBe("<pre>- a\n  - b</pre>");  // the no-DOM fallback shows the normalized input
    expect(parse("- a\n- b")).toContain("<li>a</li>");
    expect(parse("a\nb")).toContain("a<br>b");
  });
  test("an unclosed fence at EOF still renders as a code block", () => {
    expect(md("```js\nlet a = 1")).toBe("<pre>```js\nlet a = 1\n```</pre>");  // md() closes it; the no-DOM fallback shows that
    const html = parse("```js\nlet a = 1\n```");
    expect(html).toContain('<pre><code class="language-js">let a = 1');
  });
  test("without a DOM, md() falls back to escaped <pre> rather than throwing", () => {
    expect(md("**x** <y>")).toBe("<pre>**x** &lt;y&gt;</pre>");
  });
});
