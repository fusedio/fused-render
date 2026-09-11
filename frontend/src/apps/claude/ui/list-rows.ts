// Row labels and links for the landing page's Recent list — `sessionTitle`,
// `paneSlashes`, `rowPane`, `ago`, `openPaneChat`'s URL (T:18088-18225).
//
// `sessionTitle` is NOT spelled here. It lives in `protocol/history.ts`, which
// is where the wire's own vocabulary lives — `MARKER_VIEW`, `MARKER_ANN`,
// `MARKER_JOIN` and `markerWords` all come off `protocol/wire.ts` there — and it
// is re-exported below so the rows keep importing it from one place.
//
// THIS FILE USED TO CARRY A SECOND COPY, and the two had drifted in three ways
// that all reached the screen (P4-04 / B-30):
//
//   * its `MARKER_JOIN` was `" · "` where both T:10538 and `wire.ts:58` say
//     `" + "`;
//   * its marker words were invented — `"picture"` and `"comments"` instead of
//     the wire's `"pane screenshot"` and `"annotations"` — so a wordless send
//     was labelled in a vocabulary nothing else in the app used;
//   * and its openers were only the PROSE ones, so it cut nothing at a
//     surviving TAG. The stored preview is truncated, which means the closing
//     tag `stripWireBlocks` matched on is usually not in the string at all —
//     and a truncated preview then showed the literal `<pane-shot>` or
//     `<live-app-state>` as the row title. That is the exact regression T:18070
//     documents, and the same string names the snapshot run headings.
//
// `protocol/history.ts`'s copy already had all three right and was imported by
// nothing but its own test, while the live rows rendered this one.
import { urlForFsPath } from "@platform/lib/router";
import { sessionTitle } from "../protocol/history";
import type { SessionRow } from "../protocol/types";

export { sessionTitle };

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
