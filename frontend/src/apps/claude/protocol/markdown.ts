// The chat's one markdown funnel: marked → DOMPurify → innerHTML, never marked
// alone (T:14935-15055 `_mdSetup` / `renderMd` / `attachCodeCopy`). Same pins
// as the vendored libs (marked 12.0.2, DOMPurify 3.4.13, highlight.js 11.11.1).
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/common";
import { marked } from "marked";

let md: ((t: string) => string) | null = null;

/** T:14944-14972 — configured once, lazily (marked.use mutates shared state). */
function mdSetup(): (t: string) => string {
  if (md) return md;
  marked.use({
    gfm: true,
    breaks: true,
    renderer: {
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

/** Test seam: is a language registered in the bundled "common" set. */
export function hasLanguage(lang: string): boolean {
  return !!hljs.getLanguage(lang);
}
