// Markdown → HTML for a bot bubble: marked (GFM, so pipe tables, fences, task lists, autolinks) → DOMPurify →
// the thread's ONE dangerouslySetInnerHTML site (Thread.tsx HtmlMsg). Same pins and the same two rules as the
// Claude chat funnel (apps/claude/protocol/markdown.ts), which is not imported because it carries highlight.js and
// the terminal dock along with it:
//   * raw HTML the model writes is shown as TEXT (escaped), never rendered — a stray `<table>` or an unclosed
//     `<div>` must not lay out the transcript, and DOMPurify stays the security boundary rather than the only one;
//   * links open in a new tab with rel=noopener; DOMPurify's URI allow-list drops `javascript:` hrefs.
// Replaced the hand-rolled port of OpenBot's md() (no tables, so a comparison came out as a wall of `|`).
import DOMPurify from "dompurify";
import { marked } from "marked";

import { esc } from "./format";

let render: ((t: string) => string) | null = null;

/** Configured once, lazily: marked.use mutates module-wide state, and bots and the Claude chat never share a bundle. */
function setup(): (t: string) => string {
  if (render) return render;
  marked.use({
    gfm: true,
    breaks: true,
    renderer: {
      html: (html: string) => esc(html),
      // Positional (href, title, text) is marked@12's signature. Escaping here only keeps the href inside its
      // attribute; DOMPurify's document-level pass enforces which schemes survive.
      link(href: string, _title: string | null | undefined, text: string) {
        return `<a href="${esc(href || "")}" target="_blank" rel="noopener">${text}</a>`;
      },
      // A table gets its own horizontal scroller: the bubble is a `flex: none` item in a scrolling thread, so a
      // table whose min-content beats the bubble would push the whole transcript sideways. Done here rather than
      // with `display: block; overflow-x: auto` on the <table>, which takes it out of table layout and makes
      // `width: 100%` meaningless (the Claude funnel wraps in the DOM for the same reason).
      table(header: string, body: string) {
        return `<div class="tablewrap"><table><thead>${header}</thead>${body ? `<tbody>${body}</tbody>` : ""}</table></div>`;
      },
    },
  });
  render = (t) =>
    DOMPurify.sanitize(marked.parse(t, { async: false }) as string, {
      FORBID_TAGS: ["style", "form", "input", "iframe"],
      ADD_ATTR: ["target", "rel"],
    });
  return render;
}

export function md(src: unknown): string {
  // `•` bullets (OpenBot-era bot output) read as lists; an unclosed fence mid-stream closes at EOF.
  let text = String(src ?? "").replace(/\r/g, "").replace(/^(\s*)•\s+/gm, "$1- ");
  if (!text.trim()) return "";
  if (((text.match(/```/g) || []).length & 1) === 1) text += "\n```";
  try {
    return setup()(text);
  } catch {
    return `<pre>${esc(text)}</pre>`;
  }
}
