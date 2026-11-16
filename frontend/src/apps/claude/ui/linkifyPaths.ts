// Turns inline `code` that names a real file into a click target for the
// preview overlay. Runs once per FINAL markdown render, beside
// `enhanceCodeBlocks`; never on the streaming path.
//
// A guess (`pathCandidate`) is only a candidate: it is marked once a stat says
// the resolved path exists AS A FILE, so `foo.bar` or `a/b` in prose stays
// plain text. Fenced code (inside `pre`) is never touched.
import { statPath } from "@platform/lib/api";

import { pathCandidate, prosePathMatches, resolveAgainst } from "../protocol/filePaths";

export const PATH_LINK_CLASS = "c-path-link";
export const PATH_LINK_MAX = 30;

/** Linkify only a final, linked render, and only where an opener exists. */
export function linkifyEnabled(o: { links: boolean; enhance: boolean; hasOpener: boolean }): boolean {
  return o.links && o.enhance && o.hasOpener;
}

export interface LinkTarget {
  textContent: string | null;
  closest(selector: string): unknown;
  classList: { add(cls: string): void };
  setAttribute(name: string, value: string): void;
}
/** The slice of a DOM node the prose pass touches (a real Text node fits). */
export interface ProseParent {
  tagName?: string;
  classList?: { contains(cls: string): boolean };
  parentNode: ProseParent | null;
  replaceChild(next: unknown, old: unknown): unknown;
  ownerDocument?: { createElement(tag: string): ProseSpan };
}
export interface ProseSpan {
  className: string;
  setAttribute(name: string, value: string): void;
  appendChild(child: unknown): unknown;
}
export interface ProseText {
  data: string;
  parentNode: ProseParent | null;
  splitText(offset: number): ProseText;
}
export interface LinkRoot {
  querySelectorAll(selector: string): ArrayLike<LinkTarget>;
  /** Text nodes for the prose pass; defaults to a TreeWalker over a real DOM node. */
  textNodes?(): ProseText[];
  ownerDocument?: { createTreeWalker(root: unknown, what: number): { nextNode(): unknown } };
}

const PROSE_SKIP_TAGS = new Set(["PRE", "CODE", "A"]);

function proseTexts(root: LinkRoot): ProseText[] {
  if (root.textNodes) return root.textNodes();
  const doc = root.ownerDocument;
  if (!doc) return [];
  const walker = doc.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */);
  const out: ProseText[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) out.push(n as ProseText);
  return out;
}

/** Inside fenced/inline code, a link, or an already-marked path: leave alone. */
function proseBlocked(node: ProseText): boolean {
  for (let p = node.parentNode; p; p = p.parentNode) {
    if (p.tagName && PROSE_SKIP_TAGS.has(p.tagName.toUpperCase())) return true;
    if (p.classList?.contains(PATH_LINK_CLASS)) return true;
  }
  return false;
}

interface ProseHit {
  node: ProseText;
  start: number;
  end: number;
  abs: string;
}

const NEG_TTL_MS = 10_000;
const seen = new Map<string, { at: number; ok: Promise<boolean> }>();

/** Stat → "exists and is a file", shared across messages so a re-render (or a
 *  path repeated down the transcript) costs one request. A miss is retried
 *  after a few seconds: the file may be written later in the same turn. */
export function statIsFile(path: string): Promise<boolean> {
  const hit = seen.get(path);
  const now = Date.now();
  if (hit) {
    // Positives are kept; negatives expire.
    return hit.ok.then((ok) => {
      if (ok || now - hit.at < NEG_TTL_MS) return ok;
      seen.delete(path);
      return statIsFile(path);
    });
  }
  const ok = statPath(path).then(
    (r) => !r.is_dir,
    () => false,
  );
  seen.set(path, { at: now, ok });
  return ok;
}

export async function linkifyPaths(
  root: LinkRoot,
  opts: {
    cwd?: string | null;
    /** The user's home folder, for `~/x`. Unknown: such a path is not linked. */
    home?: string | null;
    stat?: (path: string) => Promise<boolean>;
    /** False once the node tree this ran against has been replaced. */
    live?: () => boolean;
  } = {},
): Promise<number> {
  const stat = opts.stat ?? statIsFile;
  const byPath = new Map<string, LinkTarget[]>();
  const proseByPath = new Map<string, ProseHit[]>();
  // Distinct admitted paths (code + prose), so one in both counts once toward the cap.
  const admitted = new Set<string>();
  for (const el of Array.from(root.querySelectorAll("code"))) {
    if (el.closest("pre")) continue;
    const cand = pathCandidate(el.textContent ?? "");
    if (!cand) continue;
    if (cand.path.startsWith("~/") && !opts.home) continue;
    const abs = resolveAgainst(cand.path, opts.cwd, opts.home);
    const list = byPath.get(abs);
    if (list) list.push(el);
    else if (byPath.size < PATH_LINK_MAX) {
      byPath.set(abs, [el]);
      admitted.add(abs);
    }
  }
  for (const node of proseTexts(root)) {
    if (node.data.length < 3 || proseBlocked(node)) continue;
    for (const m of prosePathMatches(node.data)) {
      if (m.path.startsWith("~/") && !opts.home) continue;
      const abs = resolveAgainst(m.path, opts.cwd, opts.home);
      const hit = { node, start: m.start, end: m.end, abs };
      const list = proseByPath.get(abs);
      if (list) list.push(hit);
      else if (admitted.has(abs) || admitted.size < PATH_LINK_MAX) {
        proseByPath.set(abs, [hit]);
        admitted.add(abs);
      }
    }
  }
  let marked = 0;
  const confirmed: ProseHit[] = [];
  const unique = new Set([...byPath.keys(), ...proseByPath.keys()]);
  await Promise.all(
    Array.from(unique, async (abs) => {
      if (!(await stat(abs))) return;
      if (opts.live && !opts.live()) return;
      confirmed.push(...(proseByPath.get(abs) ?? []));
      for (const el of byPath.get(abs) ?? []) {
        el.classList.add(PATH_LINK_CLASS);
        el.setAttribute("role", "button");
        el.setAttribute("tabindex", "0");
        el.setAttribute("data-path", abs);
        marked += 1;
      }
    }),
  );
  if (opts.live && !opts.live()) return marked;
  // Wrap per text node, last match first, so earlier offsets stay valid.
  const byNode = new Map<ProseText, ProseHit[]>();
  for (const h of confirmed) byNode.set(h.node, [...(byNode.get(h.node) ?? []), h]);
  for (const [node, hits] of byNode) {
    for (const h of hits.sort((a, b) => b.start - a.start)) {
      const parent = node.parentNode;
      const doc = parent?.ownerDocument;
      if (!parent || !doc || h.end > node.data.length) continue;
      const mid = node.splitText(h.start);
      mid.splitText(h.end - h.start);
      const span = doc.createElement("span");
      span.className = PATH_LINK_CLASS;
      span.setAttribute("role", "button");
      span.setAttribute("tabindex", "0");
      span.setAttribute("data-path", h.abs);
      parent.replaceChild(span, mid);
      span.appendChild(mid);
      marked += 1;
    }
  }
  return marked;
}
