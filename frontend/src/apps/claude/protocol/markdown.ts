// The chat's one markdown funnel: marked → DOMPurify → innerHTML, never marked
// alone (T:14935-15055 `_mdSetup` / `renderMd` / `attachCodeCopy`). Same pins
// as the vendored libs (marked 12.0.2, DOMPurify 3.4.13, highlight.js 11.11.1).
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/common";
import { marked } from "marked";

let md: ((t: string) => string) | null = null;

/**
 * THE RAW-HTML RULE, and there is exactly one of it (Akshil, 2026-09-08, #16):
 *
 *   > Model-authored raw HTML is shown as TEXT. It is never rendered, and that
 *   > is true in the chat, in a wall tile and in the peek popup alike.
 *
 * Before this, `<div>hi</div>` in a reply came out three different ways
 * depending on how the model happened to indent it: as an invisible empty block
 * (marked passes raw HTML straight through and DOMPurify keeps a `<div>`), as a
 * code block (four leading spaces make it one), or as literal text (inside a
 * fence). The same reply then looked like three different messages across the
 * three hosts, which is exactly the report.
 *
 * "Show it" is the right end of that choice rather than "render it":
 *   * a reply that TALKS ABOUT html — most of them, in a coding tool — wants the
 *     tag visible, which is already what a fenced block does; escaping makes the
 *     unfenced case AGREE with the fenced one instead of contradicting it;
 *   * rendering it lets the model lay out the transcript: a stray `<table>` or an
 *     unclosed `<div>` reflows the turns under it, and there is no markdown a
 *     user can type that does that;
 *   * DOMPurify still runs and is still the security boundary — it is simply no
 *     longer the only thing between a model's `<img onerror=…>` and the page.
 *
 * `renderer.html` is marked@12's hook for BOTH the block `html` token and the
 * inline `html`/`tag` tokens (node_modules/marked/lib/marked.cjs:1914 and
 * :1970), so this ONE override covers every route raw markup can take. marked's
 * own output — emphasis, links, lists, GFM tables, fences — is untouched: that
 * is markup this file generated, not markup the model wrote.
 */
function rawHtmlAsText(html: string): string {
  return escapeHtml(html);
}

/** T:14944-14972 — configured once, lazily (marked.use mutates shared state). */
function mdSetup(): (t: string) => string {
  if (md) return md;
  marked.use({
    gfm: true,
    breaks: true,
    renderer: {
      // See rawHtmlAsText above: the one raw-HTML rule, for every host.
      html: rawHtmlAsText,
      // Positional (href, title, text): marked@12's signature. DOMPurify's
      // document-level sanitize enforces the URI allow-list; this only keeps
      // the raw href from breaking out of the attribute.
      link(href: string, _title: string | null | undefined, text: string) {
        const h = (href || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;");
        return `<a href="${h}" target="_blank" rel="noopener noreferrer">${text}</a>`;
      },
    },
  });
  md = (t) =>
    DOMPurify.sanitize(marked.parse(t, { async: false }) as string, {
      FORBID_TAGS: ["style", "form", "input", "iframe"],
      ADD_ATTR: ["target", "rel"],
    });
  return md;
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** T:14974-14996. Tolerant of an unclosed fence mid-stream (odd ``` count →
 *  close it). The vendor-less `<pre>` fallback survives as the throw path. */
export function renderMd(text: string): string {
  const fences = (text.match(/```/g) || []).length;
  if (fences % 2 === 1) text += "\n```";
  try {
    return mdSetup()(text);
  } catch {
    return "<pre>" + escapeHtml(text) + "</pre>";
  }
}

/** What a `<pre>` puts on the clipboard (T:15015-15023): its `<code>`, else
 *  its per-line `<span>`s joined (an Edit chip's diff), else its text. */
function copyText(pre: HTMLElement): string {
  const code = pre.querySelector("code");
  if (code) return code.textContent ?? "";
  const lines = pre.querySelectorAll(":scope > span");
  if (lines.length) return Array.from(lines, (s) => s.textContent ?? "").join("\n");
  return pre.textContent ?? "";
}

/** T:15040-15042. */
export const COPY_RESET_MS = 1200;

/** T:14998-15055 `attachCodeCopy`: highlight `pre code.language-x` for
 *  registered languages only (idempotent on `.hljs`), then give every `<pre>`
 *  a zero-footprint `span.copywrap > button.copybtn`. Run once per FINAL
 *  render — never on the per-frame stream path. */
export function enhanceCodeBlocks(root: ParentNode): void {
  wrapTables(root);
  root.querySelectorAll<HTMLElement>("pre code").forEach((el) => {
    if (el.classList.contains("hljs")) return;
    const lang = (el.className.match(/language-(\S+)/) || [])[1];
    if (lang && hljs.getLanguage(lang)) {
      try {
        hljs.highlightElement(el);
      } catch {
        /* unhighlighted is fine */
      }
    }
  });
  root.querySelectorAll<HTMLElement>("pre").forEach((pre) => {
    if (pre.querySelector(".copybtn")) return;
    // Read BEFORE the button joins the tree, or the label rides along.
    const text = copyText(pre);
    const b = document.createElement("button");
    b.className = "copybtn";
    b.textContent = "copy";
    b.type = "button";
    let reset: ReturnType<typeof setTimeout> | null = null;
    b.onclick = () => {
      void navigator.clipboard.writeText(text);
      b.textContent = "copied";
      // One timer per button, replaced rather than stacked: a second click
      // inside the window used to leave the first timer to fire on a node the
      // re-render may already have detached.
      if (reset !== null) clearTimeout(reset);
      reset = setTimeout(() => {
        reset = null;
        if (b.isConnected) b.textContent = "copy";
      }, COPY_RESET_MS);
    };
    const wrap = document.createElement("span");
    wrap.className = "copywrap";
    wrap.appendChild(b);
    if (pre.firstChild) pre.insertBefore(wrap, pre.firstChild);
    else pre.appendChild(wrap);
  });
}

/** The class the scroll box for a GFM table carries. */
export const TABLE_WRAP_CLASS = "md-tablewrap";

/**
 * A GFM table gets its own horizontal scroller (#14).
 *
 * The table itself stays `width: 100%; border-collapse: collapse` — T's rule, and
 * the right one for a prose column: a shrink-to-fit table reads as a floating
 * fragment. But a five-column table of file paths has a min-content width the
 * 720px measure cannot honour, and without a scroller of its own it either
 * pushes the whole transcript sideways (the chat column is `min-width: 0`
 * precisely to stop that) or has its last column clipped away with no cue.
 *
 * Done in the DOM rather than in CSS because the CSS-only version of it —
 * `display: block; overflow-x: auto` on the `<table>` — takes the table box out
 * of table layout, so the rows shrink-to-fit inside an anonymous box and
 * `width: 100%` stops meaning anything. Idempotent (the wrap is checked for),
 * and it runs on the same FINAL-render pass as the code-block work for the same
 * reason: mid-stream a table is half-parsed, and re-wrapping it every frame
 * would move the box under the reader.
 */
function wrapTables(root: ParentNode): void {
  root.querySelectorAll<HTMLElement>("table").forEach((table) => {
    const parent = table.parentElement;
    if (parent && parent.classList.contains(TABLE_WRAP_CLASS)) return;
    const wrap = table.ownerDocument.createElement("div");
    wrap.className = TABLE_WRAP_CLASS;
    table.replaceWith(wrap);
    wrap.appendChild(table);
  });
}

/** Test seam: is a language registered in the bundled "common" set. */
export function hasLanguage(lang: string): boolean {
  return !!hljs.getLanguage(lang);
}
