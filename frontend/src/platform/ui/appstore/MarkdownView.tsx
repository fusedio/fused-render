// A README, rendered: GitHub-flavoured markdown (marked) → DOMPurify → one
// dangerouslySetInnerHTML site, typeset for long reading (15px/1.65, a ~680px
// measure — marketplace.css `.as-md`).
//
// RELATIVE URLS RESOLVE AGAINST `baseUrl`. A community app's readme says
// `![…](preview.png)` and `[spec](specs/design.md)`, relative to its own
// folder on GitHub; on this page those must point at the server's copy of the
// folder (`/api/marketplace/asset/<slug>/`), or every image is a broken box.
// Absolute URLs, root-relative paths, `#anchors` and `mailto:` are left alone.
//
// Raw HTML in the readme IS rendered (a centred `<p align>` banner, a
// `<details>` block — things readmes really use), with DOMPurify as the
// boundary: scripts, styles, forms, frames and event handlers never survive.
// Links open in a new tab with rel=noopener. Its own Marked instance, not the
// shared singleton, so another module's `marked.use` cannot reconfigure it.
import { useMemo } from "react";
import DOMPurify from "dompurify";
import { Marked } from "marked";

let stage: Marked | null = null;
function parser(): Marked {
  if (!stage) stage = new Marked({ gfm: true, breaks: false });
  return stage;
}

/** Scheme (`https:`), protocol-relative (`//`), root (`/`), fragment (`#`),
 *  or a data/blob URI: not ours to rewrite. */
const ABSOLUTE = /^(?:[a-z][a-z0-9+.-]*:|\/|#)/i;

export function resolveAgainst(url: string, base: string | undefined): string {
  if (!base || !url || ABSOLUTE.test(url)) return url;
  const clean = url.replace(/^\.\//, "");
  return base.replace(/\/?$/, "/") + clean;
}

/** Maps one URL found in the readme to where it should point: a string to
 *  rewrite it, `null` to drop it (an image is removed, a link becomes plain
 *  text). For content with no folder to rebase against — a `.fused` file,
 *  whose members are not served one by one. Runs after `baseUrl` rebasing. */
export type UrlResolver = (url: string, kind: "img" | "a") => string | null;

/** Markdown → sanitized HTML with relative URLs rebased. Exported for the
 *  `.fused` install page and tests. Needs a DOM (DOMPurify). */
export function renderMarkdown(source: string, baseUrl?: string, resolveUrl?: UrlResolver): string {
  const html = parser().parse(source, { async: false }) as string;
  const frag = DOMPurify.sanitize(html, {
    // Media that names its URL anywhere but `img[src]` (srcset, <source>,
    // poster) would load without passing through the resolver below.
    FORBID_TAGS: ["style", "form", "input", "iframe", "script", "object", "embed", "video", "audio", "picture", "source"],
    FORBID_ATTR: ["style", "srcset", "poster"],
    ADD_ATTR: ["target", "rel"],
    RETURN_DOM_FRAGMENT: true,
  }) as DocumentFragment;
  frag.querySelectorAll("img[src]").forEach((img) => {
    const src = resolveAgainst(img.getAttribute("src") || "", baseUrl);
    const next = resolveUrl ? resolveUrl(src, "img") : src;
    if (next === null) {
      img.remove();
      return;
    }
    img.setAttribute("src", next);
    img.setAttribute("loading", "lazy");
    img.setAttribute("decoding", "async");
  });
  frag.querySelectorAll("a[href]").forEach((a) => {
    const href = a.getAttribute("href") || "";
    if (href.startsWith("#")) return;
    const rebased = resolveAgainst(href, baseUrl);
    const next = resolveUrl ? resolveUrl(rebased, "a") : rebased;
    if (next === null) {
      a.replaceWith(...Array.from(a.childNodes));
      return;
    }
    a.setAttribute("href", next);
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noopener noreferrer");
  });
  // A table gets its own horizontal scroller so a wide one cannot push the
  // column sideways (display:block on the table itself breaks table layout).
  frag.querySelectorAll("table").forEach((t) => {
    const wrap = document.createElement("div");
    wrap.className = "as-md-tablewrap";
    t.parentNode?.insertBefore(wrap, t);
    wrap.appendChild(t);
  });
  const box = document.createElement("div");
  box.appendChild(frag);
  return box.innerHTML;
}

export interface MarkdownViewProps {
  source: string;
  /** Base for relative image/link URLs (with or without a trailing slash). */
  baseUrl?: string;
  /** Per-URL rewrite/drop (see UrlResolver). Keep its identity stable. */
  resolveUrl?: UrlResolver;
  className?: string;
}

export function MarkdownView({ source, baseUrl, resolveUrl, className }: MarkdownViewProps) {
  const html = useMemo(() => {
    try {
      return renderMarkdown(source, baseUrl, resolveUrl);
    } catch {
      return "";
    }
  }, [source, baseUrl, resolveUrl]);
  if (!html) {
    return <pre className={"as-md as-md-raw" + (className ? " " + className : "")}>{source}</pre>;
  }
  return <div className={"as-md" + (className ? " " + className : "")} dangerouslySetInnerHTML={{ __html: html }} />;
}

export default MarkdownView;
