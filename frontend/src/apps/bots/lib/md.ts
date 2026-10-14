// Markdown → HTML for a bot bubble: marked (GFM, so pipe tables, fences, task lists, autolinks) → DOMPurify →
// the thread's ONE dangerouslySetInnerHTML site (Thread.tsx HtmlMsg). Same pins and the same two rules as the
// Claude chat funnel (apps/claude/protocol/markdown.ts), which is not imported because it carries highlight.js and
// the terminal dock along with it:
//   * raw HTML the model writes is shown as TEXT (escaped), never rendered — a stray `<table>` or an unclosed
//     `<div>` must not lay out the transcript, and DOMPurify stays the security boundary rather than the only one;
//   * links open in a new tab with rel=noopener; DOMPurify's URI allow-list drops `javascript:` hrefs.
// Layout hooks the CSS and Thread.tsx lean on: `.wide` wraps a top-level block that wants the whole thread width (a 4+
// column table or a code fence with a line over 80 chars); `.capped` hides table rows past the 12th; `.tfoot` holds the
// "Show all N rows" (data-tmore) and "Copy as CSV" (data-tcsv) buttons, which Thread.tsx's delegated click handles;
// `.nw` marks a short cell that stays on one line.
// Replaced the hand-rolled port of OpenBot's md() (no tables, so a comparison came out as a wall of `|`).
import DOMPurify from "dompurify";
import { Marked, type Token, type Tokens } from "marked";

import { esc } from "./format";

let render: ((t: string) => string) | null = null;
let stage: Marked | null = null;

/** Configured once, lazily: its OWN Marked instance: `marked.use` on the shared singleton would overwrite (or be overwritten by) the Claude chat's config
 * whenever both modules load in one process (bun test). */
function setup(): (t: string) => string {
  if (render) return render;
  stage = new Marked({
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
      // Short cells stay on one line (.nw; the CSS applies it only inside a .wide table). Mirrors marked 12's own cell.
      tablecell(content: string, flags: { header: boolean; align: "center" | "left" | "right" | null }) {
        const tag = flags.header ? "th" : "td";
        const nw = content.replace(/<[^>]+>/g, "").trim().length <= 24 ? ' class="nw"' : "";
        return `<${tag}${nw}${flags.align ? ` align="${flags.align}"` : ""}>${content}</${tag}>\n`;
      },
      table(header: string, body: string) {
        return `<div class="tablewrap"><table><thead>${header}</thead>${body ? `<tbody>${body}</tbody>` : ""}</table></div>`;
      },
    },
  });
  render = (t) =>
    DOMPurify.sanitize(wrapWide(stage!.lexer(t)), {
      FORBID_TAGS: ["style", "form", "input", "iframe"],
      ADD_ATTR: ["target", "rel"],
    });
  return render;
}

const isWide = (tok: Token): boolean =>
  (tok.type === "table" && (tok as Tokens.Table).header.length >= 4) ||
  (tok.type === "code" && (tok as Tokens.Code).text.split("\n").some((l) => l.length > 80));

/** Parse the top-level tokens; each wide one gets its own `.wide` wrapper (a table also its footer), the rest stay unwrapped. */
function wrapWide(tokens: Token[]): string {
  const parse = (run: Token[]) => stage!.parser(run);
  if (!tokens.some(isWide)) return parse(tokens);
  let out = "";
  let run: Token[] = [];
  const flush = () => { if (run.length) out += parse(run); run = []; };
  for (const tok of tokens) {
    if (!isWide(tok)) { run.push(tok); continue; }
    flush();
    if (tok.type === "table") {
      const n = (tok as Tokens.Table).rows.length;
      const more = n > 12 ? `<button type="button" data-tmore="${n}">Show all ${n} rows</button>` : "";
      out += `<div class="wide${n > 12 ? " capped" : ""}">${parse([tok])}<div class="tfoot">${more}<button type="button" data-tcsv>Copy as CSV</button></div></div>`;
    } else out += `<div class="wide">${parse([tok])}</div>`;
  }
  flush();
  return out;
}

/** The marked stage alone (no sanitizer), for tests: the DOMPurify stage cannot run in bun. */
export function parseMdStage(text: string): string {
  setup();
  return wrapWide(stage!.lexer(text));
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
