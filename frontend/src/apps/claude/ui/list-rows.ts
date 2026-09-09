// Row labels and links for the landing page's Recent list — `sessionTitle`,
// `paneSlashes`, `rowPane`, `ago`, `openPaneChat`'s URL (T:18088-18225).
//
// `sessionTitle` reads a preview that may open with a WIRE BLOCK (an app-state
// snapshot, a pane shot, an annotation bundle); the block grammar itself is
// `protocol/wire.ts`'s, owned elsewhere, so only the OPENERS this label needs
// are spelled here (T:18060-18087) and the marker join is theirs.
import { urlForFsPath } from "@platform/lib/router";
import type { SessionRow } from "../protocol/types";

/** T:10527-10538 MARKER_*. */
const MARKER_JOIN = " · ";
/** The sentences `formatAnnotations` / the shot receipts always open with, and
 *  the marker that stands in for each once the words are cut (T:18060-18087). */
const BLOCK_OPENERS: readonly (readonly [string, string])[] = [
  ["The user attached ", "🖼 picture"],
  ["The user annotated ", "💬 comments"],
];

/** Everything from `<live-app-state>` on is machinery, not a title. */
function stripWireBlocks(raw: string): string {
  return raw
    .replace(/<(live-app-state|pane-shot|annotations)>[\s\S]*?<\/\1>/g, "")
    .trim();
}

/** The row's label: the user's own words, or the markers for what they sent
 *  instead of words, or the session id (T:18088-18102). */
export function sessionTitle(
  s: Pick<SessionRow, "preview" | "id"> | null | undefined,
): string {
  let text = stripWireBlocks((s && s.preview) || "");
  const carried: string[] = [];
  for (const [open, marker] of BLOCK_OPENERS) {
    const i = text.indexOf(open);
    if (i === -1) continue;
    text = text.slice(0, i).trim();
    if (marker) carried.push(marker);
  }
  return text || carried.join(MARKER_JOIN) || (s && s.id) || "";
}

/** Only a DRIVE-LETTER path has its backslashes rewritten: a backslash is a
 *  legal POSIX filename char and must round-trip (T:18104). */
export function paneSlashes(path: string): string {
  return /^[A-Za-z]:[\\/]/.test(path)
    ? String(path).replace(/\\/g, "/")
    : String(path);
}

/** The file this row's chat was opened on, or "" — and "" for a chat opened on
 *  THIS target, which is every row when the target is a file (T:18113). */
export function rowPane(s: SessionRow, file: string | null): string {
  const pane = s.pane || "";
  if (!pane) return "";
  return paneSlashes(pane) === paneSlashes(file || "") ? "" : pane;
}

/** One file in the host's left pane with one session in the Claude side — the
 *  same shell route the Tasks list opens a task on (T:18210). */
export function paneChatUrl(pane: string, sessionId: string): string {
  return urlForFsPath(
    paneSlashes(pane),
    `?_side=claude&session_id=${encodeURIComponent(sessionId)}`,
  );
}

/** T:17947-17953. */
export function ago(ts: number, now: number = Date.now()): string {
  const s = Math.max(0, now / 1000 - ts);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 172800) return "yesterday";
  return `${Math.floor(s / 86400)}d ago`;
}
