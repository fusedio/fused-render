// THE CHATS IN THIS FOLDER THAT HAVE NOT RUN YET — the landing's Recent list,
// with the queue's own rows folded into it (prefs `queue.enabled`).
//
// THE GAP. "Recent chats" is the transcripts in this folder's project dir
// (agent.py `_sessions`): a chat is in the list once the CLI has written its
// first row. A chat whose first message was QUEUED has written nothing — nothing
// of it has run — so the one place a reader could see what they typed was the
// pane they typed it in, and a reload, a Back, or opening the folder from
// anywhere else showed a landing with no sign of it at all. The words were safe
// on the server the whole time; the landing simply had no row for them.
//
// THE ROW EXISTS, IT IS JUST ON ANOTHER LIST. `/api/tasks` carries every waiting
// task, keyed `pending:<leader entry id>` until its run opens a session
// (routers/tasks.py). So this reads that listing, keeps the rows that belong to
// this folder, and shapes them as the SessionRow the list already draws — one
// list, in one order, with one row per conversation.
//
// NOT A SECOND SECTION, and that is the whole design: a chat that is waiting is
// not a different kind of thing from a chat that ran, it is the same
// conversation earlier on. It sorts by `last_active` among the rest and is told
// apart by one word (`waiting`, where a live one says `running`) in the state's
// own gold.
import { getTasks, type Task } from "@platform/lib/api";
import { useEffect, useRef, useState } from "react";
import {
  CHAT_ENTRY_ORIGIN,
  chatUrl,
  PENDING_KEY_PREFIX,
  pendingEntryId,
} from "@platform/lib/queue";
import { TASKS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { changeIsHere } from "../protocol/sessions";
import type { SessionRow } from "../protocol/types";

/** The two statuses a task that has never run can wear. `queued` is the project
 *  queue's own word (something else holds the folder); `upcoming` is a message
 *  that is simply due later. Anything else has run, and has a transcript for the
 *  ordinary list to find. */
export const WAITING_TASK_STATUSES = new Set(["queued", "upcoming"]);

/**
 * THE ONE ORIGIN THAT IS A CONVERSATION — `"chat"`, stamped by
 * `POST /api/tasks/queue/admit` and by nothing else (`Task.entry_origin`).
 *
 * This is the whole difference between "a chat that has not run yet" and "a job
 * that is due on Thursday". Both are `pending:<entry>` rows with a waiting
 * status; only one of them is something somebody typed into a composer and will
 * come back to read. Listing on the key alone would have put every future
 * scheduled job, every repeat's next occurrence and every New-task form entry
 * into Recent CHATS — a list whose whole promise is "what has happened here".
 *
 * IT GATES `upcoming` AND NOT `queued` (Akshil, 2026-09-12). The two waiting
 * statuses are not the same fact about time: `upcoming` is "due later", which is
 * a diary entry whatever composed it, while `queued` is DUE NOW AND HELD — the
 * message would be running this second if the folder were free. A New-task
 * modal's message that lands in a busy folder is a conversation a reader has
 * just started and will come back to in a minute; leaving it off this list
 * because a form rather than a composer typed it hid the very rows the queue
 * exists to explain.
 */
export const CHAT_ORIGIN = CHAT_ENTRY_ORIGIN;

/** Only what a waiting row is built from, so a test hands over a handful of
 *  fields rather than the whole of `Task`. */
export type WaitingTask = Pick<
  Task,
  | "key"
  | "status"
  | "project"
  | "target"
  | "title"
  | "task_id"
  | "last_active"
  | "entry_id"
  | "entry_origin"
> &
  /** OPTIONAL, unlike on `Task`: only the merge's dedupe reads it, and a caller
   *  (or a test) holding a row from before it existed is not wrong. */
  Partial<Pick<Task, "session_id">>;

/**
 * The waiting chats of THIS folder, newest first, as list rows.
 *
 * `file` is the pane's target and may be a FILE inside the project, which is why
 * the folder test is `changeIsHere` (the same rule the Recent list's own
 * change-poll uses) rather than string equality: a task on `~/x` belongs on the
 * landing of `~/x/notes.md`.
 */
export function waitingChatRows(
  tasks: readonly WaitingTask[] | null | undefined,
  file: string | null,
): SessionRow[] {
  if (!file) return [];
  const out: SessionRow[] = [];
  for (const task of tasks || []) {
    if (!task || !task.key) continue;
    // THE SERVER NAMES THE ENTRY (`entry_id`); the key is the fallback for a
    // server too old to. A TASK THAT HAS RUN IS NOT ONE OF THESE either way —
    // the store rekeys a task onto its Claude session the moment its run opens
    // one, so the `pending:` prefix is the test for "no transcript yet", and a
    // row that has one is already in the list this merges into.
    const entry = (task.entry_id || "").trim() || pendingEntryId(task.key);
    if (!entry || !pendingEntryId(task.key)) continue;
    if (!WAITING_TASK_STATUSES.has(task.status)) continue;
    // …AND, FOR `upcoming`, ONLY A CHAT'S OWN (`CHAT_ORIGIN`): a calendar
    // message and a repeat's next occurrence are waiting `pending:` rows too,
    // and a thing due on Thursday is a diary entry rather than a conversation.
    // `queued` is exempt, because it says something `upcoming` does not — this
    // is due NOW and the folder is busy — and that is a chat waiting to start
    // whichever box composed it (see CHAT_ORIGIN).
    if (task.status !== "queued" && (task.entry_origin || "") !== CHAT_ORIGIN) continue;
    const project = task.project || "";
    const target = task.target || project;
    if (!changeIsHere(project, file) && !changeIsHere(target, file)) continue;
    out.push({
      // The KEY is the id, so the row is stable across reads and cannot collide
      // with a real session id (which never carries a colon).
      id: PENDING_KEY_PREFIX + entry,
      preview: task.title || "",
      created_at: task.last_active || 0,
      last_used: task.last_active || 0,
      cwd: project,
      // The file the chat is about, blank when it is about the folder — the same
      // thing `taskFile` answers, and what `rowPane` compares against.
      pane: target && target !== project ? target : "",
      // NEVER `running`: a waiting chat is the opposite of a running one, and
      // the row's own word says which (`queuedEntry`).
      running: false,
      queuedEntry: entry,
      taskId: task.task_id || "",
      // THE SESSION ITS LEADER HAS OPENED, when the listing already knows one —
      // the second half of the dedupe below. "" for every row that has not run.
      leaderSession: (task.session_id || "").trim(),
      href: chatUrl(target || project, "", entry),
    });
  }
  return out;
}

/**
 * The two lists as one, newest first.
 *
 * `null` is preserved: it is the skeleton state and means "the sessions read has
 * not answered", which stays true whatever the tasks read said. The waiting rows
 * are an ADDITION to that list and never a reason to stop waiting for it.
 *
 * A waiting row whose id is already in the list is dropped rather than drawn
 * twice — the window where a leader's run has opened a session, the transcript
 * exists, and this read is one lap stale.
 *
 * …AND SO IS ONE WHOSE LEADER'S SESSION IS ALREADY THERE (`leaderSession`,
 * 🔴 review 2026-09-12). That is the same window seen from the other side, and
 * it is the one that actually happened on screen: the run opens a session, the
 * transcript lands, `sessions` lists it by its SESSION id — while the tasks read
 * still holds the row under `pending:<entry>`. Two ids, one conversation, two
 * rows in the list until the next tasks read. The transcript row wins: it is the
 * one with a transcript behind it.
 */
export function mergeWaitingChats(
  recent: SessionRow[] | null,
  waiting: readonly SessionRow[],
): SessionRow[] | null {
  if (recent === null) return null;
  if (!waiting.length) return recent;
  const seen = new Set(recent.map((s) => s.id));
  const rows = recent.concat(
    waiting.filter((w) => !seen.has(w.id) && !(w.leaderSession && seen.has(w.leaderSession))),
  );
  // BY `last_active`, which is `last_used` on a row — the list's own order, so
  // a waiting chat lands where its age puts it and not in a clump at either end.
  return rows.sort((a, b) => (b.last_used || 0) - (a.last_used || 0));
}

/**
 * The tasks listing behind the rows above, re-read on every "something moved".
 *
 * ONE READ AND A POKE, deliberately not a poll: `/api/tasks` is already fetched
 * by half this page, the announcement (`fused-render:tasks-changed`) is what the
 * admission itself rings on the very send that creates one of these rows, and
 * the landing is a view a reader passes through rather than watches.
 *
 * Answers `[]` — never `null` — because the absence of waiting chats is not a
 * state worth a skeleton: the list it merges into owns that.
 */
export function useWaitingChats(
  file: string | null,
  enabled: boolean,
  /**
   * THE RECENT LIST'S OWN TICK — whatever the caller's session list publishes,
   * read only for its identity (🔴 review 2026-09-12).
   *
   * `tasks-changed` is rung by an admission and by a run's own start, and the
   * moment this pair of lists disagrees about is neither: a leader's transcript
   * appearing is news the SESSIONS watch hears (`/api/tasks/changes` long-poll,
   * protocol/sessions) and this read did not. The list then held a waiting row
   * for a chat that was already drawn as a transcript until something unrelated
   * poked it. So every tick of that watch re-reads the tasks too, and the two
   * halves of one list are never more than one lap apart.
   */
  tick?: unknown,
): SessionRow[] {
  const [rows, setRows] = useState<SessionRow[]>([]);
  /** The live reader, so the tick effect below can fire it without owning the
   *  subscription's lifetime. */
  const readRef = useRef<() => void>(() => {});
  useEffect(() => {
    if (!enabled || !file) {
      setRows([]);
      readRef.current = () => {};
      return;
    }
    let live = true;
    const read = () => {
      void getTasks()
        .then((data) => {
          if (!live) return;
          setRows(waitingChatRows(data.tasks || [], file));
        })
        .catch(() => {
          // The list stands: a folder whose tasks cannot be read reads as a
          // folder with no waiting chats, exactly as `subscribeRecent` treats a
          // failed sessions read.
        });
    };
    readRef.current = read;
    read();
    const onPoke = () => read();
    window.addEventListener(TASKS_CHANGED_EVENT, onPoke);
    window.addEventListener("focus", onPoke);
    return () => {
      live = false;
      readRef.current = () => {};
      window.removeEventListener(TASKS_CHANGED_EVENT, onPoke);
      window.removeEventListener("focus", onPoke);
    };
  }, [file, enabled]);
  // …and the tick. FIRST RUN SKIPPED: the effect above has just read, and two
  // identical reads on mount is a doubled request for nothing.
  const firstTick = useRef(true);
  useEffect(() => {
    if (firstTick.current) {
      firstTick.current = false;
      return;
    }
    readRef.current();
  }, [tick]);
  return rows;
}
