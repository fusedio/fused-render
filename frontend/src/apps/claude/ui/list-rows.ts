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
import { chatDraftKey, readChatDraft } from "@platform/lib/drafts";
import type { DraftAttachment } from "@platform/lib/drafts";
import { urlForFsPath } from "@platform/lib/router";
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
 * A DRAFT ROW'S CONTENT, FOR THE COMPOSER THE READER IS ALREADY LOOKING AT
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
 * WHERE THE CONTENT COMES FROM is the one asymmetry between the two kinds:
 *
 *   * a CHAT draft is stored under `new:<file>` and the row carries only a
 *     `preview` of it — a first line, clipped (`fused_render/drafts.preview`)
 *     — so the WHOLE RECORD is fetched, text and tray together, through the
 *     same door the composer's own seed effect uses;
 *   * a TASK draft carries its whole stored form on the row already (`form`,
 *     the field the modal used to reopen on), so its description — or its
 *     title, for a title-only form — is read straight off it.
 *
 * `whole` IS THE FIELD THAT MATTERS TO A MOVE (Bugbot #1166). It used to
 * answer the row's clipped preview when the fetch failed, on the reasoning
 * that half a sentence beats an empty box — right for a COPY and wrong for a
 * move, because the caller then deleted the full record it had never managed
 * to read and kept 120 characters of it. So the two answers are told apart:
 * `whole: false` is "this is a stand-in", and a caller that is about to
 * destroy the source may not act on one.
 */
export interface DraftContent {
  /** What to put in the box. */
  text: string;
  /** The source draft's tray — a chat draft's stored attachment rows, and `[]`
   *  for a task draft, whose files belong to the form and not to a composer. */
  attachments: DraftAttachment[];
  /** Is this the record itself, rather than the row's clipped stand-in? */
  whole: boolean;
}

export async function draftContentOf(task: Task): Promise<DraftContent> {
  if (isChatDraftTask(task)) {
    const key = chatDraftKey(null, task.file || task.target || "");
    const { draft, read } = await readChatDraft(key);
    if (draft) {
      return { text: draft.text || "", attachments: draft.attachments ?? [], whole: true };
    }
    // READ and EMPTY is a whole answer — there is nothing under this key, and a
    // caller moving it has nothing to lose. A read that never answered is not.
    if (read) return { text: "", attachments: [], whole: true };
    return { text: task.draft?.preview || "", attachments: [], whole: false };
  }
  const form = (task.form ?? {}) as { description?: unknown; title?: unknown };
  const described = String(form.description ?? "").trim();
  const text = described || String(form.title ?? task.title ?? "").trim();
  return { text, attachments: [], whole: true };
}

/**
 * THE JOIN A COMPOSER MAKES when words arrive in a box that is not empty —
 * spelled once, here, because two callers now have to agree about it.
 *
 * `Composer`'s `restore` seat appends rather than replaces, which is the right
 * way round for a box that may already hold something the reader typed: a press
 * must never eat words. `ClaudeChat.onFillDraft` has to predict the same result
 * one line earlier, because on a MOVE it writes the destination draft to the
 * server BEFORE dropping the source, and what it writes must be what the box is
 * about to hold (Bugbot #1166). Two copies of a one-line rule is how the record
 * and the box start disagreeing.
 *
 * A single newline, and only when there is something to join to. (The
 * PROGRAMMATIC send's seed uses a blank line instead — a paragraph boundary the
 * model reads — and that one is `Composer.submit`'s own, deliberately not this.)
 */
export function joinIntoBox(prev: string, back: string): string {
  if (!back) return prev;
  return prev.trim() ? prev.replace(/\s*$/, "\n") + back : back;
}

/**
 * IS PRESSING THIS DRAFT ROW A MOVE, OR A REQUEST FOR THE BOX? (design.md,
 * PR C.)
 *
 * A press puts the row's words in the composer, and the composer has a draft key
 * of its own — the folder it is mounted on — which its next autosave writes them
 * under. So for every row but ONE the press is a MOVE: leaving the source where
 * it was would make one sentence two rows, in two folders, with two TASK
 * numbers, and whichever the reader finished the other would still be sitting
 * there unsent.
 *
 * The one exception is THIS composer's own draft. That row is not a source to
 * move from — it is the box already on screen, drawn as a row — so its press is
 * a focus request and nothing else.
 *
 * A TASK draft moves too, as of the live repro (bugbot, 2026-09-15): a build
 * that read its form's words into the box WITHOUT moving it left both records
 * behind — the task draft's own row, unchanged, AND a brand-new `new:<file>`
 * chat draft the composer's autosave minted under the words it had just
 * copied. One press must leave exactly one record, so a task draft's press is
 * a move like any other — `onFillDraft` reuses the same PUT-then-discard path,
 * and `discardDraft` already branches on the draft's own kind to delete it
 * correctly either way.
 */
export function draftMovesOut(task: Task, file: string | null): boolean {
  if (!isDraftTask(task)) return false; // an ordinary conversation is not a draft at all
  if (isChatDraftTask(task)) return task.key !== chatDraftKey(null, file);
  return true;
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
