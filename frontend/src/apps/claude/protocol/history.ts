// A restored conversation, and the strings a session row wears.
//
// `T`'s `loadHistory` (T:17988-18060) is half renderer and half protocol; this
// is the protocol half — the payload turned into `Turn[]` — so the React side
// only has to draw. The rules it keeps:
//
//   * a USER turn's display text is `stripBlocks(t.text)`, and the RAW text is
//     kept beside it: the "what was sent" popover rebuilds its receipt out of
//     that wire alone (T:18029, T:10932).
//
//     WHICH BLOCKS ARE STILL ON THAT WIRE IS THE SERVER'S CALL, and it is not
//     all of them. `agent.py`'s `_history` runs `_strip_app_state` over every
//     user row BEFORE the payload is built ("the user never typed it and never
//     saw it"), so `<live-app-state>` cannot reach this function — `stripBlocks`
//     is a no-op for it here and `raw === text` for a turn that pushed app
//     state. `<pane-shot>` and `<annotations>` DO survive, which is exactly why
//     those two receipts rebuild after a reload (`ui/Receipts.tsx`) and the
//     app-state line does not.
//
//     So `appState` is deliberately NOT set here (R1-1, reviewed and rejected
//     2026-09-10): there is no evidence of the block in the payload to read it
//     off, and drawing the line without the wire would be an inert
//     "app state attached" over a panel that can only show the bubble back —
//     the lie `receipt-door.test.tsx` already forbids. Verified against a real
//     transcript with nine app-state sends: `_history` returns none of them.
//     Restoring that receipt needs a flag from the server, which is a Python
//     change and a separate piece of work.
//   * an assistant turn carries `segments` only when it had any; a text-only
//     turn has no such key at all (agent.py:5041, T:18024).
//   * `stopped` is written by agent.py `_stopped_last` on the LAST turn only,
//     and only when true (agent.py:5049) — it renders as the same ⏹ "Stopped."
//     note a live stop leaves, so a conversation reads identically whether you
//     watched the stop or came back to it (T:18040-18048).
//   * `uuid` is the transcript record's own id — the value the Tasks list
//     carries as a message `anchor`, which is what makes `?msg=` resolvable
//     (T:18030). Optional: a payload without it simply cannot be anchored to.
import type { Turn } from "./controller-api";
import { troubleFromMessage } from "./trouble";
import type { HistoryResponse, HistoryTurn, SessionRow } from "./types";
import {
  ANN_TAG,
  APP_STATE_TAG,
  MARKER_ANN,
  MARKER_JOIN,
  MARKER_VIEW,
  PANE_SHOT_TAG,
  markerWords,
  stripBlocks,
} from "./wire";

/** T:17988-18052 — the payload as transcript rows. Indexed keys, because a
 *  restored turn has no seat of its own and `uuid` is optional. */
export function historyToTurns(resp: HistoryResponse): Turn[] {
  const rows = Array.isArray(resp?.turns) ? resp.turns : [];
  const last = rows.length - 1;
  return rows.map((t: HistoryTurn, i: number) => {
    if (t.role === "user") {
      return {
        role: "user" as const,
        key: t.uuid || "h:" + i,
        text: stripBlocks(t.text),
        raw: t.text,
        ...(t.uuid ? { uuid: t.uuid } : {}),
      };
    }
    // BEFORE the assistant fallback, which is unconditional: a role this
    // mapper does not know becomes an assistant turn, so a failed turn used to
    // render as the model's own prose after a reload while the live run had
    // shown it in red (feedback R2-3/R2-14). `ErrorTurn.kind` is required, and
    // `troubleFromMessage` is the same classifier the live path runs the poll's
    // `error` through — so a restored failure carries the same kind, and the
    // red row (`Turn.tsx`, which branches on `role`) is identical either way.
    if (t.role === "error") {
      return {
        role: "error" as const,
        key: "h:" + i,
        text: t.text || "",
        kind: troubleFromMessage(t.text || "", true).kind,
      };
    }
    if (t.stopped && i !== last && typeof console !== "undefined") {
      console.warn(
        "[chat] history carried `stopped` on turn " + i + " of " + last + "; only the last " +
          "turn's stop is rendered (agent.py:5049)",
      );
    }
    return {
      role: "assistant" as const,
      key: "h:" + i,
      text: t.text || "",
      ...(Array.isArray(t.segments) ? { segments: t.segments } : {}),
      // Only ever on the last turn — guarded here too, so a payload that ever
      // carried it earlier cannot print a second stop note (agent.py:5049).
      // The guard is ADDED over T:18040, which is unguarded, so it says so when
      // it fires: silently dropping the flag would make a payload change look
      // like a rendering bug rather than a contract that moved.
      ...(t.stopped && i === last ? { stopped: true as const } : {}),
    };
  });
}

// T:18075-18081 — the second pass `sessionTitle` needs and `stripBlocks` cannot
// do alone: the stored preview is TRUNCATED, so the closing tag every strip
// matches on is usually not in the string at all. Cut from any surviving
// opener, and from the annotation block's tag-less preamble.
const BLOCK_OPENERS: [string, string][] = [
  ["<" + APP_STATE_TAG + ">", ""],
  ["<" + PANE_SHOT_TAG + ">", MARKER_VIEW],
  // Tag-less by construction (see stripAnnBlock) — matched on the one sentence
  // formatAnnotations always opens with.
  ["The user annotated ", MARKER_ANN],
];

/** T:18088 `sessionTitle` — what a session row (and a snapshot heading) is
 *  LABELLED with. Never blank: a row with no title is a row nobody can pick out
 *  of a list, so the markers, then the id, stand in.
 *
 *  A LABEL, so the markers arrive as their words: the sigil that tells a marker
 *  apart from a typed "files" belongs to the bubble's own detector, and a row
 *  title carrying an invisible format character is a title nothing else — a
 *  filter, a document title — can match (`markerWords`, wire.ts). */
export function sessionTitle(s: Pick<SessionRow, "id" | "preview"> | null | undefined): string {
  const raw = (s && s.preview) || "";
  let text = stripBlocks(raw);
  const carried: string[] = [];
  for (const [open, marker] of BLOCK_OPENERS) {
    const i = text.indexOf(open);
    if (i === -1) continue;
    text = text.slice(0, i).trim();
    if (marker) carried.push(marker);
  }
  return markerWords(text || carried.join(MARKER_JOIN)) || (s && s.id) || "";
}

/** T:17945 `ago` — epoch SECONDS in, a relative phrase out. `now` is injectable
 *  for tests; the template reads `Date.now()`. */
export function ago(ts: number, now: () => number = Date.now): string {
  const s = Math.max(0, now() / 1000 - ts);
  if (s < 60) return "now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  if (s < 172800) return "yesterday";
  return Math.floor(s / 86400) + "d ago";
}

/** Unused by `sessionTitle` itself but part of its contract — exported so a UI
 *  that has to explain a marker-only title reads the same list. */
export const TITLE_BLOCK_OPENERS: readonly (readonly [string, string])[] = BLOCK_OPENERS;

/** T:18106 `rowPane` needs this: only a drive-letter path has its backslashes
 *  rewritten, because a backslash is a legal POSIX filename char. */
export function paneSlashes(path: string): string {
  return /^[A-Za-z]:[\\/]/.test(path) ? String(path).replace(/\\/g, "/") : String(path);
}

/** T:18113 `rowPane` — the file this row's chat was opened on, or "" for one
 *  opened on THIS target (naming the same file on every row is noise). */
export function rowPane(s: Pick<SessionRow, "pane"> | null | undefined, file: string | null): string {
  const pane = (s && s.pane) || "";
  if (!pane) return "";
  return paneSlashes(pane) === paneSlashes(file || "") ? "" : pane;
}

/** The tag name of the annotations block, re-exported so a caller explaining a
 *  title does not have to import from two files. */
export { ANN_TAG };
