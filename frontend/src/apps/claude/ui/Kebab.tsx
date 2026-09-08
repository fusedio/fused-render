// More options (⋮) — the chat's one menu (T:4003-4032 markup,
// T:13120-13470 behaviour, inventory 04 §F).
//
// Four items, and every label is decided at OPEN time because each names
// something that changes without a reload: the session param (the terminal
// item's verb), and the task behind this chat (whether the archive item exists
// at all, and which way it reads). `applyArchiveOpt` draws from what the page
// already knows, SYNCHRONOUSLY, so the item is in the menu's first paint;
// `refreshArchiveOpt` then re-reads the listing and corrects a WORD rather than
// the menu's height (T:13130-13140, 2026-08-24).
import { useCallback, useEffect, useRef, useState } from "react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@platform/shadcn/ui/dropdown-menu";
import {
  archiveTask,
  getTasks,
  unarchiveTask,
  type Task,
} from "@platform/lib/api";
import { runAgent } from "../protocol/agent";
import type { TerminalCommandResponse } from "../protocol/types";

/** Live by the listing's own clock (T:13166-13169). */
const RUNNING_STATES = new Set<Task["status"]>([
  "in_progress",
  "needs_attention",
]);

/** Page-level, like T's `archiveStates` / `taskIds` / `taskRunning`: the answer
 *  outlives one open, and `undefined` ("not asked") is NOT `null` ("there is no
 *  task") — unknown keeps the items hidden, because inventing a verb for a task
 *  we have not confirmed is how a menu offers to archive something that is not
 *  there (T:13141-13160). */
const archiveStates = new Map<string, boolean | null>();
const taskIds = new Map<string, string>();
const taskRunning = new Map<string, boolean>();

/** These outlive one chat and one mount, so they are CAPPED: a shell left open
 *  for a day visits a great many sessions, and three entries each for all of
 *  them is a leak with no upper bound. Oldest first — the answer for the session
 *  on screen is the one just written. */
const TASK_CACHE_MAX = 200;

function remember<V>(map: Map<string, V>, key: string, value: V): void {
  map.set(key, value);
  while (map.size > TASK_CACHE_MAX) {
    const oldest = map.keys().next().value as string;
    map.delete(oldest);
  }
}

/** Test/host seam: the caches are keyed by session id and a deleted session
 *  must not leave a stale answer behind (T:13348-13351). */
export function forgetTaskCaches(sessionId: string): void {
  archiveStates.delete(sessionId);
  taskIds.delete(sessionId);
  taskRunning.delete(sessionId);
}

/** One `/api/tasks` read per session id, ever: a task number does not change
 *  once allocated, so a poll would buy nothing (T:12660-12672).
 *
 *  A SET OF WAITERS, not a bare "in flight" flag. A session really does get
 *  read by two mounts at once — a card and its own TaskPeek are exactly that —
 *  and a flag made the second mount skip the read AND the `bump` that follows
 *  it, so that one sat on the session hash until something unrelated
 *  re-rendered it. The read is still one read; every mount waiting on it is
 *  woken when it lands. */
const inflight = new Map<string, Set<() => void>>();

/**
 * T:12696 `showSession` + T:12705 `loadTaskId` — THE TASK NUMBER FOR THIS CHAT,
 * read on entering a session rather than on opening the menu.
 *
 * The number is what the header prints (`#session`) and what the delete confirm
 * names, and the same listing read answers the kebab's "is there a task behind
 * this chat, and which way is it filed" — which is why T does one read here and
 * has the menu paint from what it already knows (T:12681-12684).
 *
 * FAILS OPEN to the session hash, like everything else that reads this listing:
 * an unreadable `/api/tasks`, or a session so new the listing has not seen it,
 * costs the better name and never the cell. Nothing is cached on that path, so
 * the next session change tries again.
 */
export function useTaskId(sessionId: string): string {
  const [, bump] = useState(0);
  useEffect(() => {
    if (!sessionId || taskIds.has(sessionId)) return;
    let live = true;
    const wake = () => {
      if (live) bump((n) => n + 1);
    };
    // Someone else is already asking: join their wake-up list rather than
    // firing a second identical read (and rather than silently going without an
    // answer, which is what a bare flag did).
    const joined = inflight.get(sessionId);
    if (joined) {
      joined.add(wake);
      return () => {
        live = false;
        joined.delete(wake);
      };
    }
    const waiters = new Set<() => void>([wake]);
    inflight.set(sessionId, waiters);
    void getTasks()
      .then((data) => {
        const task = (data.tasks || []).find((t) => t && t.key === sessionId);
        // Recorded BEFORE the number check, and the order matters: a task with
        // no number allocated yet is still a task the kebab can archive, and
        // `null` is the real answer "this chat is not a task" (T:12712-12718).
        remember(archiveStates, sessionId, task ? task.status === "archived" : null);
        remember(taskRunning, sessionId, !!task && RUNNING_STATES.has(task.status));
        if (task?.task_id) remember(taskIds, sessionId, String(task.task_id));
      })
      .catch(() => {
        // The hash stands.
      })
      .finally(() => {
        inflight.delete(sessionId);
        // Every mount that was waiting, not just the one that asked.
        for (const waiter of [...waiters]) waiter();
      });
    return () => {
      live = false;
      waiters.delete(wake);
    };
  }, [sessionId]);
  if (!sessionId) return "";
  // The hash paints first and stays up until the number lands, so the answer to
  // a slow listing is the old label rather than a gap (T:12698).
  return taskIds.get(sessionId) || sessionId.slice(0, 8);
}

/** What the listing knows about this chat, for the caller's own reads (the
 *  erase dialog wants the number). */
export function knownTaskId(sessionId: string): string | undefined {
  return taskIds.get(sessionId);
}

export interface KebabProps {
  agentDir: string | null;
  file: string | null;
  sessionId: string;
  /** The trigger, so the erase dialog can put focus back where it came from on
   *  every close path (T:13293, 13319-13321). */
  btnRef?: React.MutableRefObject<HTMLElement | null>;
  /** The landing has no session, so nothing in this menu can act — but the
   *  affordance stays where it always was (T's `#kebab` rides `#anntools`,
   *  which BOTH views keep) rather than appearing out of nowhere on entering a
   *  chat. Disabled, and saying why. */
  disabled?: boolean;
  /** This page's own turn: `body.running`'s replacement. Live by EITHER clock
   *  — the listing's word or ours (T:13166). */
  running: boolean;
  /** Open the erase confirm (this item opens it and NOTHING else, T:13315). */
  onErase(): void;
  /** Show the raw outgoing text of the last user turn, when there is one. */
  onWhatWasSent?(): void;
}

export function Kebab({
  agentDir,
  file,
  sessionId,
  btnRef,
  disabled,
  running,
  onErase,
  onWhatWasSent,
}: KebabProps) {
  const [open, setOpen] = useState(false);
  /** Bumped whenever a cache write should repaint the items. */
  const [rev, setRev] = useState(0);
  const [terminalLabel, setTerminalLabel] = useState("");
  const [archiveLabel, setArchiveLabel] = useState("");
  /** A press in flight, or its confirmation still on screen, OWNS its item:
   *  a listing read landing in that window must not overwrite the words
   *  (T:13143-13150). */
  const busy = useRef(false);
  const timers = useRef<number[]>([]);

  useEffect(
    () => () => {
      for (const id of timers.current) window.clearTimeout(id);
    },
    [],
  );
  const later = useCallback((fn: () => void, ms: number) => {
    const id = window.setTimeout(() => {
      // Spliced on the way out, so the list is what is still PENDING rather
      // than everything this mount has ever scheduled.
      const i = timers.current.indexOf(id);
      if (i >= 0) timers.current.splice(i, 1);
      fn();
    }, ms);
    timers.current.push(id);
  }, []);

  const filed = sessionId ? archiveStates.get(sessionId) : undefined;
  const hasTask = !!sessionId && filed !== undefined && filed !== null;
  const live = (!!sessionId && !!taskRunning.get(sessionId)) || running;

  /** Re-read the listing and correct the item. FAILS CLOSED, unlike the task
   *  number read: an item offered on a listing we could not read is an item
   *  whose verb we are guessing at — and nothing is written on that path, so a
   *  previously confirmed reading STANDS (T:13181-13211). */
  // The LIVE session id, for the in-flight guard below: comparing the captured
  // value with a copy of itself can never fire.
  const liveSession = useRef(sessionId);
  liveSession.current = sessionId;

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    const id = sessionId;
    try {
      const data = await getTasks();
      const task = (data.tasks || []).find((t) => t && t.key === id);
      // The session can change while this is in flight (the reader clicked a
      // recent chat as the menu opened); a stale answer must not label the item
      // for a conversation nobody is looking at any more (T:13195).
      if (liveSession.current !== id) return;
      remember(archiveStates, id, task ? task.status === "archived" : null);
      remember(taskRunning, id, !!task && RUNNING_STATES.has(task.status));
      if (task?.task_id) remember(taskIds, id, task.task_id);
      if (!busy.current) setRev((n) => n + 1);
    } catch {
      // Fails closed; see the note above.
    }
  }, [sessionId]);

  const onOpenChange = useCallback(
    (next: boolean) => {
      setOpen(next);
      if (!next) return;
      // Named at open time: the session param changes without a reload
      // (T:13415).
      setTerminalLabel(
        sessionId ? "Continue in terminal" : "New session in terminal",
      );
      setArchiveLabel("");
      void refresh();
    },
    [sessionId, refresh],
  );

  const restingArchive = filed ? "Unarchive this task" : "Archive this task";

  const onTerminal = useCallback(async () => {
    if (!agentDir) return;
    busy.current = true;
    try {
      const out = (await runAgent(
        agentDir,
        "terminal_command",
        { file: file ?? "", session_id: sessionId },
        { key: null },
      )) as TerminalCommandResponse;
      if ("error" in out && out.error) throw new Error(out.error);
      if (!("command" in out)) throw new Error("agent.py returned no command");
      await navigator.clipboard.writeText(out.command);
      // The copied state shows INSIDE the item, then the menu goes away on its
      // own: the click's whole job was the clipboard (T:13454).
      setTerminalLabel("Copied — paste in your terminal");
      later(() => {
        busy.current = false;
        setOpen(false);
      }, 900);
    } catch (err) {
      setTerminalLabel(
        `Copy failed — ${err instanceof Error ? err.message : String(err)}`,
      );
      later(() => {
        busy.current = false;
        setTerminalLabel(
          sessionId ? "Continue in terminal" : "New session in terminal",
        );
      }, 2500);
    }
  }, [agentDir, file, sessionId, later]);

  const onArchive = useCallback(async () => {
    if (!sessionId || !hasTask) return;
    const wasFiled = !!filed;
    busy.current = true;
    try {
      const body = wasFiled
        ? await unarchiveTask(sessionId)
        : await archiveTask(sessionId);
      // Archiving CANCELS this task's pending work as well as filing it, and
      // the count comes back in the answer — so the confirmation says what
      // actually happened rather than a bare "done" (T:13236-13243).
      const off = wasFiled
        ? 0
        : Number((body as { cancelled?: number }).cancelled) || 0;
      setArchiveLabel(
        wasFiled
          ? "Unarchived"
          : off
            ? `Archived — ${off} pending run${off === 1 ? "" : "s"} cancelled`
            : "Archived",
      );
      // The cached listing is now WRONG for this session, and the next open
      // would paint the old verb from it (T:13244-13250).
      remember(archiveStates, sessionId, !wasFiled);
      later(() => {
        busy.current = false;
        setArchiveLabel("");
        setOpen(false);
        setRev((n) => n + 1);
      }, 1100);
    } catch (err) {
      setArchiveLabel(
        `Could not ${wasFiled ? "unarchive" : "archive"} — ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
      // `busy` stays UP until the message has had its 2.5s: it is what keeps a
      // listing read from wiping the failure off the button (T:13268).
      later(() => {
        busy.current = false;
        setArchiveLabel("");
        void refresh();
      }, 2500);
    }
  }, [sessionId, hasTask, filed, later, refresh]);

  // `rev` is read so a cache write repaints the items it decides.
  void rev;

  // THE LANDING'S KEBAB. Nothing in the menu can act on a chat that does not
  // exist yet, so the control is inert — but it is DRAWN, because in T the
  // kebab rides the `#anntools` strip that both the landing and the transcript
  // keep (T:3842, T:4003), and an affordance that appears from nowhere on
  // entering a chat reads as the header growing a button.
  if (disabled) {
    return (
      <div className="c-kebab">
        <button
          type="button"
          className="c-kebabbtn"
          aria-label="More options"
          title="More options"
          disabled
          ref={(el) => {
            if (btnRef) btnRef.current = el;
          }}
        >
          ⋮
        </button>
      </div>
    );
  }

  return (
    <div className="c-kebab">
      <DropdownMenu open={open} onOpenChange={onOpenChange}>
        <DropdownMenuTrigger
          render={
            <button
              type="button"
              className="c-kebabbtn"
              aria-label="More options"
              title="More options"
              ref={(el) => {
                if (btnRef) btnRef.current = el;
              }}
            >
              ⋮
            </button>
          }
        />
        <DropdownMenuContent
          side="bottom"
          align="end"
          sideOffset={6}
          aria-label="More options"
          className="c-overlay c-kebabpop w-auto min-w-[196px] rounded-[10px] bg-[var(--c-panel)] p-1 text-[var(--c-fg)] shadow-none ring-0"
        >
          <DropdownMenuItem
            className="c-kebab-opt"
            closeOnClick={false}
            onClick={() => void onTerminal()}
          >
            {terminalLabel ||
              (sessionId ? "Continue in terminal" : "New session in terminal")}
          </DropdownMenuItem>
          {onWhatWasSent ? (
            <DropdownMenuItem className="c-kebab-opt" onClick={onWhatWasSent}>
              What was sent
            </DropdownMenuItem>
          ) : null}
          {/* HIDDEN, not disabled, when there is no task behind the chat: a
              disabled row invites the reader to work out what would enable it,
              and the answer is not something they can act on from this menu
              (T:13100-13108). */}
          {hasTask ? (
            <DropdownMenuItem
              className="c-kebab-opt"
              closeOnClick={false}
              disabled={live}
              title={live ? "Stop the run first" : undefined}
              onClick={() => void onArchive()}
            >
              {archiveLabel || restingArchive}
            </DropdownMenuItem>
          ) : null}
          {hasTask ? (
            <DropdownMenuItem
              className="c-kebab-opt is-danger"
              disabled={live}
              title={live ? "Stop the run first" : undefined}
              onClick={onErase}
            >
              Delete this task
            </DropdownMenuItem>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
