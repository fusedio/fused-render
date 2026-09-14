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
import { chatDraftKey, fetchChatDraft } from "@platform/lib/drafts";
import { urlForFsPath } from "@platform/lib/router";
import { isChatDraftTask } from "@shell/tasks-lib";
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
 * A DRAFT ROW'S WORDS, FOR THE COMPOSER THE READER IS ALREADY LOOKING AT
 * (Akshil, 2026-09-15).
 *
 * A draft row used to be a DOOR: a chat draft about another file hopped the
 * host to that file's chat, and a task draft left the app entirely for
 * `/tasks?draft=<id>`. Both were navigations away from the landing page in
 * answer to a press on a list that sits UNDER the landing's own composer —
 * which is the one box those words belong in. So neither goes anywhere now:
 * the press fills the composer and puts the caret after the text, and the
 * reader decides what to do with it from there.
 *
 * WHERE THE WORDS COME FROM is the one asymmetry between the two kinds:
 *
 *   * a CHAT draft is stored under `new:<file>` and the row carries only a
 *     `preview` of it — a first line, clipped (`fused_render/drafts.preview`)
 *     — so the full text is FETCHED, through the same door the composer's own
 *     seed effect uses. The preview stands in only if that read answers
 *     nothing, because half a sentence is better than an empty box;
 *   * a TASK draft carries its whole stored form on the row already (`form`,
 *     the field the modal used to reopen on), so its description — or its
 *     title, for a title-only form — is read straight off it.
 */
export async function draftTextOf(task: Task): Promise<string> {
  if (isChatDraftTask(task)) {
    const key = chatDraftKey(null, task.file || task.target || "");
    const saved = await fetchChatDraft(key);
    return saved?.text || task.draft?.preview || "";
  }
  const form = (task.form ?? {}) as { description?: unknown; title?: unknown };
  const described = String(form.description ?? "").trim();
  if (described) return described;
  return String(form.title ?? task.title ?? "").trim();
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
