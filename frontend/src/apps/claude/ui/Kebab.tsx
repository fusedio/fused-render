// More options (⋮) — the chat's one menu (T:4003-4032 markup,
// T:13120-13470 behaviour, inventory 04 §F).
//
// THREE items, exactly the template's: the terminal hand-off, Archive/Unarchive
// and Delete this task. "What was sent" was a fourth here for one revision and
// is gone (Akshil, 2026-09-08): T opens that panel from a RECEIPT ROW under the
// bubble it belongs to, where "what was sent" names one turn — in a menu that
// hangs off the whole conversation the same three words name nothing in
// particular, and the reader has to guess which turn they would get.
//
// ON THE LANDING PAGE the menu is REAL and has one item — "New session in
// terminal" (T's `#kebabpop` home state, T:13415). It was drawn inert here,
// which is the one thing a control must never be: the item that does exist
// there needs no session, it hands the FOLDER to a terminal.
//
// Every label is decided at OPEN time because each names
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
import { EraseTaskModal } from "@platform/ui/EraseTaskModal";
import { TASKS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
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

/** One `/api/tasks` read per session id ONCE THE NUMBER HAS LANDED: a task
 *  number does not change after it is allocated, so a poll would buy nothing
 *  (T:12660-12672). Before it lands is the opposite case — see `ASK_AGAIN_MS`.
 *
 *  A SET OF WAITERS, not a bare "in flight" flag. A session really does get
 *  read by two mounts at once — a card and its own TaskPeek are exactly that —
 *  and a flag made the second mount skip the read AND the `bump` that follows
 *  it, so that one sat on the session hash until something unrelated
 *  re-rendered it. The read is still one read; every mount waiting on it is
 *  woken when it lands. */
const inflight = new Map<string, Set<() => void>>();

/**
 * WHEN TO ASK AGAIN, and it is not "never" (R2-9).
 *
 * "One read, ever" was right about a number that exists and wrong about the
 * moment this hook is most often called: a chat that has just started has a
 * session id seconds before `/api/tasks` has a row for it, so the one read came
 * back with nothing, nothing was cached, and the header printed a truncated
 * session hash until the reader reloaded the page (Akshil, 2026-09-08 — "shows
 * the session id instead of TASK-xxx, and only updates after reload").
 *
 * So the read repeats, on a DECAYING schedule and only while the answer is
 * still missing — four asks over about fifteen seconds, which covers the
 * scheduler allocating the row, and then it stops. It is not a poll: the moment
 * a number lands the schedule is torn down and never runs again for that
 * session, and every other source of an answer (the tasks-changed event, this
 * document's own chat-activity stamp) short-circuits the wait.
 */
export const ASK_AGAIN_MS = [900, 2500, 5000, 8000];

/** ClaudeChat's `CHAT_ACTIVITY_KEY`, restated rather than imported: that module
 *  imports this one through `ui/index`, and a chat cannot be made to depend on
 *  its own menu to know the name of a localStorage key. One string, in two
 *  places, that has not changed since T:16435. */
const CHAT_ACTIVITY_KEY = "fused-render:chat-activity";

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
    const timers: number[] = [];
    const wake = () => {
      if (live) bump((n) => n + 1);
    };

    /** One read, shared with any other mount asking for the same session in the
     *  same window. Resolves when the answer (or the failure) has landed. */
    const ask = (): Promise<void> => {
      // Someone else is already asking: join their wake-up list rather than
      // firing a second identical read (and rather than silently going without
      // an answer, which is what a bare flag did).
      const joined = inflight.get(sessionId);
      if (joined) {
        joined.add(wake);
        return Promise.resolve();
      }
      const waiters = new Set<() => void>([wake]);
      inflight.set(sessionId, waiters);
      return getTasks()
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
    };

    /** Ask, and — while the answer is still missing — arrange to ask again.
     *  `at` is the index into `ASK_AGAIN_MS`, so the schedule decays and ENDS;
     *  a session that never becomes a task stops costing reads. */
    const askThen = (at: number) => {
      void ask().then(() => {
        if (!live || taskIds.has(sessionId)) return;
        const wait = ASK_AGAIN_MS[at];
        if (wait === undefined) return;
        timers.push(window.setTimeout(() => askThen(at + 1), wait));
      });
    };
    askThen(0);

    // THE TWO PUSHES, either of which beats the timer above. `tasks-changed` is
    // announced by this document's own run controller the moment a turn starts
    // (protocol/run-controller.ts), which is the same moment the row is created;
    // the storage stamp is every OTHER document's chat saying the same thing
    // (ClaudeChat's CHAT_ACTIVITY_KEY). Both are pokes, not payloads, so both
    // land on one handler.
    const poke = () => {
      if (!live || taskIds.has(sessionId)) return;
      void ask();
    };
    const onStorage = (ev: StorageEvent) => {
      if (!ev.key || ev.key === CHAT_ACTIVITY_KEY) poke();
    };
    window.addEventListener(TASKS_CHANGED_EVENT, poke);
    window.addEventListener("storage", onStorage);
    return () => {
      live = false;
      for (const id of timers) window.clearTimeout(id);
      window.removeEventListener(TASKS_CHANGED_EVENT, poke);
      window.removeEventListener("storage", onStorage);
      inflight.get(sessionId)?.delete(wake);
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
  /** The landing page's menu: ONE item, "New session in terminal", which is
   *  all T offers there (T:13415) and all that can mean anything without a
   *  session. The ⋮ rides the same seat in both views (T's `#kebab` is on the
   *  `#anntools` strip both keep) so it never appears out of nowhere on
   *  entering a chat. */
  landing?: boolean;
  /** This page's own turn: `body.running`'s replacement. Live by EITHER clock
   *  — the listing's word or ours (T:13166). */
  running: boolean;
  /** The session is GONE. Every cache keyed by it is dropped here (this menu
   *  owns them), so the caller only has to leave the transcript — "Back to
   *  chats" IS the way out (T:13352-13366). */
  onErased?(sessionId: string): void;
}

export function Kebab({
  agentDir,
  file,
  sessionId,
  btnRef,
  landing,
  running,
  onErased,
}: KebabProps) {
  const [open, setOpen] = useState(false);
  const [erasing, setErasing] = useState(false);
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

  /**
   * THE ROW BEHIND THIS CHAT, RE-READ WHENEVER IT MOVES (R3-2).
   *
   * `refresh` used to run only on OPEN, and a menu corrected that late argues
   * with itself: `taskRunning` is cached TRUE for the whole of a turn, the popup
   * reads `disabled` as it mounts its items, and a listing read landing 200 ms
   * into the open does not lift it — so Archive and Delete stayed greyed out
   * after the run had finished and only came back on a SECOND open (owner, R3-2:
   * "kebab Archive/Delete stayed disabled after done").
   *
   * So the cache is corrected BEFORE the menu can be opened, on every signal
   * that says the row moved:
   *   * `running` in the deps — our own turn starting and ending, which this
   *     page knows one render before any listing does;
   *   * `tasks-changed` — this document's run controller, which announces at
   *     both ends of every turn (`noteChatActivity`);
   *   * `storage` — every OTHER document's chat saying the same (T:16435).
   *
   * `useTaskId` cannot do this job and is not asked to: it stops asking the
   * moment the NUMBER lands, which is precisely when the STATUS starts
   * mattering. This is also what makes the two items APPEAR at all for a chat
   * whose task row was created after that hook went quiet — `hasTask` is read
   * off the same map.
   *
   * One `/api/tasks` read per turn boundary, and it FAILS CLOSED exactly as
   * `refresh` does everywhere else: a listing we could not read leaves the
   * previously confirmed reading standing rather than guessing a new one.
   */
  useEffect(() => {
    if (!sessionId) return;
    let live = true;
    const poke = () => {
      if (live) void refresh();
    };
    poke();
    const onStorage = (ev: StorageEvent) => {
      if (!ev.key || ev.key === CHAT_ACTIVITY_KEY) poke();
    };
    window.addEventListener(TASKS_CHANGED_EVENT, poke);
    window.addEventListener("storage", onStorage);
    return () => {
      live = false;
      window.removeEventListener(TASKS_CHANGED_EVENT, poke);
      window.removeEventListener("storage", onStorage);
    };
  }, [sessionId, running, refresh]);

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

  /**
   * THE MENU GOES FIRST (R2-8). This used to hold the dropdown open through the
   * whole round trip so the item could report back inside itself — "Archived —
   * 2 pending runs cancelled", then close on a 1.1s timer. Pressing a menu item
   * and having the menu STAY reads as the press not having registered (Akshil,
   * 2026-09-08); a reader who clicks Archive has finished with the menu, and
   * every millisecond it lingers is the app arguing about that.
   *
   * So: close, flip the cached verb OPTIMISTICALLY (the next open reads
   * `archiveStates`, and the answer that matters is the one the reader just
   * chose), then run the call. The confirmation the item used to carry is not
   * lost — it is now redundant, because the Tasks page's own row moves.
   *
   * A FAILURE STILL HAS TO LAND SOMEWHERE. The flip is put back and the menu
   * REOPENS with the error on the item that earned it: silently reverting would
   * leave the reader believing a task is filed when it is not, which is the one
   * outcome worse than a menu that came back.
   */
  const onArchive = useCallback(async () => {
    if (!sessionId || !hasTask) return;
    const wasFiled = !!filed;
    // Optimistic, and before the await: `hasTask`/`restingArchive` are read off
    // this map, so the reopened menu — or the next one — says the new word.
    remember(archiveStates, sessionId, !wasFiled);
    setArchiveLabel("");
    setOpen(false);
    setRev((n) => n + 1);
    busy.current = false;
    try {
      if (wasFiled) await unarchiveTask(sessionId);
      else await archiveTask(sessionId);
    } catch (err) {
      // Put the world back before saying anything about it.
      remember(archiveStates, sessionId, wasFiled);
      if (liveSession.current !== sessionId) return;
      busy.current = true;
      setArchiveLabel(
        `Could not ${wasFiled ? "unarchive" : "archive"} — ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
      setOpen(true);
      // `busy` stays UP until the message has had its 2.5s: it is what keeps a
      // listing read from wiping the failure off the item (T:13268).
      later(() => {
        busy.current = false;
        setArchiveLabel("");
        void refresh();
      }, 2500);
    }
  }, [sessionId, hasTask, filed, later, refresh]);

  // `rev` is read so a cache write repaints the items it decides.
  void rev;

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
          {/* HIDDEN, not disabled, when there is no task behind the chat: a
              disabled row invites the reader to work out what would enable it,
              and the answer is not something they can act on from this menu
              (T:13100-13108). */}
          {!landing && hasTask ? (
            <DropdownMenuItem
              className="c-kebab-opt"
              /* R2-8 — the press closes the menu. `onArchive` closes it too
                 (the state is ours, and a controlled `open` has to be told),
                 but leaving this at `false` meant a click landed on a menu that
                 stayed put for as long as the flip took. */
              disabled={live}
              title={live ? "Stop the run first" : undefined}
              onClick={() => void onArchive()}
            >
              {archiveLabel || restingArchive}
            </DropdownMenuItem>
          ) : null}
          {!landing && hasTask ? (
            <DropdownMenuItem
              className="c-kebab-opt is-danger"
              disabled={live}
              title={live ? "Stop the run first" : undefined}
              onClick={() => setErasing(true)}
            >
              Delete this task
            </DropdownMenuItem>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
      {/* THE TASKS PAGE'S OWN DIALOG (Akshil, 2026-09-08). The chat had a
          confirm of its own with the same two sentences re-typed into it — one
          irreversible action, two designs, and the copy free to drift. What was
          in the way was the layering rule (an app may not import from `shell`),
          so the component moved to `platform/ui` and both surfaces now share
          it: the title names the task, the body says what goes and that it
          cannot come back, and the 409 lands verbatim inside the dialog on the
          button that earned it. */}
      {erasing ? (
        <EraseTaskModal
          task={{ key: sessionId, task_id: taskIds.get(sessionId) || "this task" }}
          // THE CHAT'S TOKENS, ON A DIALOG THAT PORTALS OUT OF THE CHAT
          // (FIX-17). This is the same component the Tasks page renders, so it
          // paints from the SHELL's `--error`/`--fg`/`--fg-muted` — and outside
          // `.chat-root` there is nothing to say the chat's values instead: the
          // danger ink read `rgb(255,107,107)` where the template reads
          // `rgb(242,109,109)`, from tokens that are byte-identical on both
          // sides. `.c-tokens` publishes the chat's block plus a three-token
          // bridge (`styles/chat.css`); the Tasks page's copy passes nothing
          // and is untouched.
          dialogClassName="c-tokens"
          onClose={() => {
            setErasing(false);
            // Opened from a menu item rather than a trigger, so nothing gives
            // focus back on its own (T:13293, 13319-13321).
            btnRef?.current?.focus();
          }}
          onDone={() => {
            setErasing(false);
            // Every cache keyed by this session is now a lie (T:13348-13366).
            forgetTaskCaches(sessionId);
            onErased?.(sessionId);
          }}
        />
      ) : null}
    </div>
  );
}
