// Row labels and links for the landing page's Recent list — `sessionTitle`,
// `paneSlashes`, `taskPane`, `taskInPane`, `ago`, `openPaneChat`'s URL
// (T:18088-18225).
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
import type { Task } from "@platform/lib/api";
import { urlForFsPath } from "@platform/lib/router";
import { schedulerUrl } from "../sched/scheduled";
import { isChatDraftTask, isDraftTask } from "@shell/tasks-lib";
import { sessionTitle } from "../protocol/history";

export { sessionTitle };

/** Only a DRIVE-LETTER path has its backslashes rewritten: a backslash is a
 *  legal POSIX filename char and must round-trip (T:18104). */
export function paneSlashes(path: string): string {
  return /^[A-Za-z]:[\\/]/.test(path)
    ? String(path).replace(/\\/g, "/")
    : String(path);
}

/** Trailing slashes off, so a folder that arrives spelled either way compares
 *  equal to itself (tasks-lib.taskFile asks the same of the same two fields). */
function trimSlash(path: string): string {
  return paneSlashes(path || "").replace(/\/+$/, "");
}

/**
 * The file this row's chat was opened on, or "" — and "" for a chat opened on
 * THIS target, which is every row when the target is a file (T:18113).
 *
 * A TASK's answer, now that the Recent list draws task rows (.claude-design/
 * design.md §B). The two halves are the same two the old `rowPane` asked of a
 * `SessionRow`, spelled against the fields `/api/tasks` carries:
 *
 *   * WHICH FILE the chat is about is `target`, unless `target` IS the folder —
 *     the server resolves a folder-scoped task's target to its project, so
 *     "target is not project" is the whole test (tasks-lib.taskFile, restated
 *     rather than imported so this module keeps its one shell import);
 *   * and a file that is THIS pane's own file is not another pane at all.
 */
export function taskPane(
  task: { target?: string; project?: string },
  file: string | null,
): string {
  const target = trimSlash(task.target || "");
  if (!target || target === trimSlash(task.project || "")) return "";
  return target === trimSlash(file || "") ? "" : (task.target as string);
}

/**
 * IS THIS TASK ABOUT THE PANE THE LIST IS IN? (.claude-design/design.md §B:
 * "filtered `project === folder` / file pane `target === file`".)
 *
 * ONE test for both panes rather than a branch on "is the target a file", which
 * is a question this side cannot answer without a stat: a FOLDER pane matches
 * through `project` (every task in it, whichever file it is about), and a FILE
 * pane matches through `target` (only the chats about that document). A task
 * can only match a file pane through `project` if the pane path IS a project
 * folder, in which case it is a folder pane and the answer is right anyway.
 */
export function taskInPane(
  task: { target?: string; project?: string },
  pane: string | null,
): boolean {
  const here = trimSlash(pane || "");
  if (!here) return false;
  return trimSlash(task.target || "") === here || trimSlash(task.project || "") === here;
}

/** One file in the host's left pane with one session in the Claude side — the
 *  same shell route the Tasks list opens a task on (T:18210). */
export function paneChatUrl(pane: string, sessionId: string): string {
  return urlForFsPath(
    paneSlashes(pane),
    `?_side=claude&session_id=${encodeURIComponent(sessionId)}`,
  );
}

/**
 * WHERE A CHAT DRAFT'S "BACK TO CHAT" LANDS — the conversation the words belong
 * to, derived from the row rather than from whoever pressed it.
 *
 * A row knows its own folder (`file`/`target`) and, when the draft is a
 * session's, the thread to resume; the presser knows neither, and asking it for
 * a route only means every view has to answer the same question its own way.
 */
export function draftChatUrl(task: Task): string {
  const at = task.file || task.target || "";
  if (!at) return "";
  // A `new:<file>` row has no session to continue; a row keyed on one does, and
  // naming it is what makes the chat open on that thread rather than on a blank
  // landing beside it.
  return paneChatUrl(at, task.key.includes(":") ? "" : task.key);
}

/**
 * WHERE A DRAFT ROW'S PRESS GOES — THE NEW TASK MODAL, FROM EVERYWHERE (Akshil,
 * 2026-09-16, superseding design "one record" §1's two doors).
 *
 * A press OPENS the record where it already lives; it never copies it, never
 * moves it, and never mints a second one. What changed is that BOTH kinds of
 * draft now open the same card:
 *
 *   * a CHAT draft (`new:<file>`, or a session's) is pressed through the hop the
 *     composer's own Schedule button builds — `?new=1&draft=<key>&target=&from=`
 *     — so the card opens on that very record, pre-filled, and "Back to chat"
 *     lands in the folder it came from;
 *   * a TASK draft is `/tasks?draft=<id>`, which is the same card by id.
 *
 * WHY ONE DOOR. A draft row looks identical in all six places it is drawn, and
 * it used to do two different things depending on what was behind it: a chat
 * draft navigated to a composer, a task draft opened a form, and the row in the
 * composer's OWN folder did neither — it asked for the keyboard. Three
 * behaviours behind one affordance is three things to learn, and the composer
 * arm was the one nobody could predict from looking. The record is the same
 * either way, the card edits it in place, and the composer goes on autosaving
 * the same key — so the press costs a URL and nothing is minted.
 *
 * WHAT THIS REPLACED before that was a MOVE: the press read the source record
 * whole, wrote its words into the composer the reader happened to be looking at,
 * and deleted the source. It had to be read-whole-or-refuse, guarded against a
 * second press landing mid-move, ordered against the destination's own
 * autosave, and undone when any step failed — five mechanisms in aid of a press
 * that now costs a URL, because the one thing a move existed to prevent (one
 * sentence, two rows, two TASK numbers) cannot happen if nothing is ever copied.
 */
export function draftHref(task: Task): string | null {
  if (!isDraftTask(task)) return null;
  if (isChatDraftTask(task)) {
    // The FOLDER, stated rather than derived: a session key spells no path at
    // all, and a card that has to guess one guesses the reader's home.
    const at = task.project || task.file || task.target || "";
    return schedulerUrl(task.key, draftChatUrl(task), at);
  }
  return task.draft_id ? `/tasks?draft=${encodeURIComponent(task.draft_id)}` : null;
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
