// Pure helpers behind the in-chat file preview overlay: which files this
// session wrote, whether a piece of inline code looks like a path, and how a
// relative path resolves against the chat's working folder.
import { isDriveAbs } from "@platform/lib/path-url";
import type { Segment } from "./types";

const EDIT_TOOLS = new Set(["Write", "Edit", "MultiEdit", "NotebookEdit"]);

/** Any turn: only assistant turns carry `segments`. */
type HasSegments = object;

function collect(segs: readonly Segment[] | undefined, out: string[]): void {
  if (!Array.isArray(segs)) return;
  // Newest first: later segments are visited before earlier ones.
  for (let i = segs.length - 1; i >= 0; i--) {
    const s = segs[i];
    if (!s || s.kind !== "tool") continue;
    // A subagent's own transcript ran BEFORE its parent call settled.
    collect(s.children, out);
    if (!EDIT_TOOLS.has(s.name)) continue;
    const inp = (s.input || {}) as Record<string, unknown>;
    const p = s.name === "NotebookEdit" ? inp.notebook_path : inp.file_path;
    if (typeof p === "string" && p) out.push(p);
  }
}

/** The files this session wrote or edited, newest first, deduped. */
export function touchedFiles(turns: ReadonlyArray<HasSegments>): string[] {
  const all: string[] = [];
  for (let i = turns.length - 1; i >= 0; i--) {
    collect((turns[i] as { segments?: Segment[] } | undefined)?.segments, all);
  }
  return Array.from(new Set(all));
}

/** POSIX `/x`, a Windows drive path (`C:\x`, `C:/x`) or a UNC path (`\\host\x`). */
export function isAbsolutePath(path: string): boolean {
  return path.startsWith("/") || isDriveAbs(path) || path.startsWith("\\\\");
}

/** Absolute paths pass through; a relative one joins `cwd`, with `.`/`..`
 *  folded. No cwd leaves a relative path as it is. */
export function resolveAgainst(path: string, cwd: string | null | undefined): string {
  if (isAbsolutePath(path) || !cwd) return path;
  const parts: string[] = [];
  for (const seg of (cwd.replace(/\/+$/, "") + "/" + path).split("/")) {
    if (!seg || seg === ".") continue;
    if (seg === "..") parts.pop();
    else parts.push(seg);
  }
  return "/" + parts.join("/");
}

export interface PathCandidate {
  path: string;
  line?: number;
}

/** Does inline code look like a file path? A trailing `:line[:col]` is peeled
 *  off. Only a guess: the caller stats it before marking anything. */
export function pathCandidate(text: string): PathCandidate | null {
  const t = text.trim();
  if (!t || t.length > 300 || /\s/.test(t)) return null;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(t)) return null;
  if (/[<>{}()*?|"'`$\\~]/.test(t)) return null;
  const m = /^(.*?)(?::(\d+)(?::\d+)?)?$/.exec(t);
  const path = m?.[1] ?? "";
  if (!path || path.endsWith("/")) return null;
  if (!path.includes("/") && !/\.[A-Za-z0-9]{1,8}$/.test(path)) return null;
  return m?.[2] ? { path, line: Number(m[2]) } : { path };
}
