// Turns inline `code` that names a real file into a click target for the
// preview overlay. Runs once per FINAL markdown render, beside
// `enhanceCodeBlocks`; never on the streaming path.
//
// A guess (`pathCandidate`) is only a candidate: it is marked once a stat says
// the resolved path exists AS A FILE, so `foo.bar` or `a/b` in prose stays
// plain text. Fenced code (inside `pre`) is never touched.
import { statPath } from "@platform/lib/api";

import { pathCandidate, resolveAgainst } from "../protocol/filePaths";

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
export interface LinkRoot {
  querySelectorAll(selector: string): ArrayLike<LinkTarget>;
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
    stat?: (path: string) => Promise<boolean>;
    /** False once the node tree this ran against has been replaced. */
    live?: () => boolean;
  } = {},
): Promise<number> {
  const stat = opts.stat ?? statIsFile;
  const byPath = new Map<string, LinkTarget[]>();
  for (const el of Array.from(root.querySelectorAll("code"))) {
    if (el.closest("pre")) continue;
    const cand = pathCandidate(el.textContent ?? "");
    if (!cand) continue;
    const abs = resolveAgainst(cand.path, opts.cwd);
    const list = byPath.get(abs);
    if (list) list.push(el);
    else if (byPath.size < PATH_LINK_MAX) byPath.set(abs, [el]);
  }
  let marked = 0;
  await Promise.all(
    Array.from(byPath, async ([abs, els]) => {
      if (!(await stat(abs))) return;
      if (opts.live && !opts.live()) return;
      for (const el of els) {
        el.classList.add(PATH_LINK_CLASS);
        el.setAttribute("role", "button");
        el.setAttribute("tabindex", "0");
        el.setAttribute("data-path", abs);
        marked += 1;
      }
    }),
  );
  return marked;
}
