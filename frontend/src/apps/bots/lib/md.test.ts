import { describe, expect, test } from "bun:test";
import { md } from "./md";

describe("md", () => {
  test("escapes before formatting", () => {
    expect(md("<script>alert(1)</script>")).toBe("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>");
    expect(md('a "b" & \'c\'')).toBe("<p>a &quot;b&quot; &amp; &#39;c&#39;</p>");
  });
  test("empty and nullish input", () => {
    expect(md("")).toBe("");
    expect(md(null)).toBe("");
    expect(md(undefined)).toBe("");
  });
  test("inline: bold, italic, code", () => {
    expect(md("**bold** and *it* and _it2_")).toBe("<p><b>bold</b> and <i>it</i> and <i>it2</i></p>");
    expect(md("snake_case_name stays")).toBe("<p>snake_case_name stays</p>");
    expect(md("2 * 3 * 4")).toBe("<p>2 * 3 * 4</p>");
  });
  test("code spans are protected from other rules", () => {
    expect(md("`**x** <y>`")).toBe("<p><code>**x** &lt;y&gt;</code></p>");
    expect(md("`a` then `b`")).toBe("<p><code>a</code> then <code>b</code></p>");
  });
  test("links: markdown and bare", () => {
    expect(md("[site](https://x.io/a)")).toBe('<p><a href="https://x.io/a" target="_blank" rel="noopener">site</a></p>');
    expect(md("see https://x.io/b.")).toBe('<p>see <a href="https://x.io/b" target="_blank" rel="noopener">https://x.io/b</a>.</p>');
    expect(md("[js](javascript:alert(1))")).toBe("<p>[js](javascript:alert(1))</p>");
  });
  test("paragraph lines join with <br>, blank lines split", () => {
    expect(md("a\nb\n\nc")).toBe("<p>a<br>b</p><p>c</p>");
    expect(md("a\r\nb")).toBe("<p>a<br>b</p>");
  });
  test("headings, rules, blockquotes", () => {
    expect(md("# One\n### Three ###")).toBe("<h1>One</h1><h3>Three</h3>");
    expect(md("---")).toBe("<hr>");
    expect(md("> quoted *x*")).toBe("<blockquote>quoted <i>x</i></blockquote>");
  });
  test("lists: ul, ol, switching kinds, wrapped items", () => {
    expect(md("- a\n- b")).toBe("<ul><li>a</li><li>b</li></ul>");
    expect(md("1. a\n2) b")).toBe("<ol><li>a</li><li>b</li></ol>");
    expect(md("- a\n1. b")).toBe("<ul><li>a</li></ul><ol><li>b</li></ol>");
    expect(md("• a\n  continued")).toBe("<ul><li>a continued</li></ul>");
    expect(md("text\n- item")).toBe("<p>text</p><ul><li>item</li></ul>");
    expect(md("- item\ntext")).toBe("<ul><li>item</li></ul><p>text</p>");
  });
  test("fences keep content verbatim (escaped) and close at EOF", () => {
    expect(md("```\n**x** <b>\n  y\n```")).toBe("<pre><code>**x** &lt;b&gt;\n  y</code></pre>");
    expect(md("```js\nlet a = 1")).toBe("<pre><code>let a = 1</code></pre>");
    expect(md("para\n```\ncode\n```\nafter")).toBe("<p>para</p><pre><code>code</code></pre><p>after</p>");
  });
  test("a URL inside a code span is not linked", () => {
    expect(md("`https://x.io/a`")).toBe("<p><code>https://x.io/a</code></p>");
  });
});
