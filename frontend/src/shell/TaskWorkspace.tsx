// The Workspace view: every task that is not closed, newest first, each one a
// card saying what it is doing and what keeps it open (workspace-lib.ts), with a
// close button, an Undo toast and a keyboard map. Its rules live in
// workspace-lib; this file draws them and runs the two polls the rows need
// (ports and holds), asked for the visible rows only.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  archiveTask,
  fetchTaskHolds,
  fetchTaskPorts,
  unarchiveTask,
} from "@platform/lib/api";
import type { Task, TaskHold } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import { peekPrHead, usePrStatuses } from "@platform/lib/prStatus";
import { navigateUrl } from "@platform/lib/router";
import { prKey, prRefsOf } from "@platform/lib/task-prs";
import { WorkspaceSearch } from "@shell/WorkspaceSearch";
import { closePeek, openPeek, peekItemProps, PEEK_OPEN_CLASS, usePeekedKey } from "@shell/task-peek-store";
import { useNow } from "@platform/lib/clock";
import { IdChip, IdentityChip, PrNumChip, StatusIcon } from "@shell/ScheduleTaskViews";
import {
  basename,
  liveJobRow,
  openThreadIntent,
  peekOpenable,
  ringFailed,
  spansProjects,
  taskColumn,
  taskWhenFor,
  tildePath,
} from "@shell/tasks-lib";
import {
  bgJobChips,
  capList,
  cardStatusWord,
  MAX_PRS,
  messagePreview,
  holdReasons,
  moveSelection,
  needsYou,
  nextNeedingYou,
  pickBranch,
  visibleReasons,
  workspaceKeyAction,
  workspaceTasks,
} from "@shell/workspace-lib";

/** How long the Undo toast stays up after a close. */
export const UNDO_MS = 8000;
const POLL_MS = 15000;
/** The server answers at most this many ids per read. */
const MAX_IDS = 100;

function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== "string") return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable === true;
}

/** Ports and holds for `keys`, refreshed on a timer while the page is visible. */
function useRowFacts(keys: string[]): {
  ports: Record<string, number[]>;
  holds: Record<string, TaskHold[]>;
  covered: ReadonlySet<string>;
  failed: boolean;
} {
  const [ports, setPorts] = useState<Record<string, number[]>>({});
  const [holds, setHolds] = useState<Record<string, TaskHold[]>>({});
  const [covered, setCovered] = useState<ReadonlySet<string>>(new Set());
  const [failed, setFailed] = useState(false);
  const joined = keys.slice(0, MAX_IDS).join("\n");
  useEffect(() => {
    const ids = joined ? joined.split("\n") : [];
    if (ids.length === 0) return;
    let dead = false;
    const tick = (): void => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      fetchTaskPorts(ids)
        .then((p) => {
          if (!dead) setPorts(p);
        })
        .catch(() => {});
      fetchTaskHolds(ids)
        .then((h) => {
          if (dead) return;
          setHolds(h);
          setCovered((prev) => new Set([...prev, ...ids]));
          setFailed(false);
        })
        .catch(() => {
          if (!dead) setFailed(true);
        });
    };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => {
      dead = true;
      window.clearInterval(id);
    };
  }, [joined]);
  return { ports, holds, covered, failed };
}

function PrChips({ task, states }: { task: Task; states: ReturnType<typeof usePrStatuses> }) {
  const refs = prRefsOf(task).filter((r) => r.repo);
  if (refs.length === 0) return null;
  const { shown, more } = capList(refs, MAX_PRS);
  return (
    <span className="ws-prs">
      {shown.map((r) => {
        const state = states[prKey(r)];
        return (
          <span key={prKey(r)} className="ws-pr" title={state ?? undefined}>
            <PrNumChip prRef={r} status={state ?? ""} />
          </span>
        );
      })}
      {more > 0 && <span className="ws-pr-more">+{more}</span>}
    </span>
  );
}

function WorkspaceCard({
  task,
  selected,
  peeked,
  peekOn,
  ports,
  reasons,
  holdRows,
  states,
  showProject,
  home,
  now,
  onSelect,
  onOpen,
  onClose,
}: {
  task: Task;
  selected: boolean;
  peeked: boolean;
  peekOn: boolean;
  ports: number[];
  /** The full hold list; the card prints the visible part and "clear" on none. */
  reasons: string[];
  holdRows: TaskHold[];
  states: ReturnType<typeof usePrStatuses>;
  /** More than one project on the page: the preview wears the folder chip. */
  showProject: boolean;
  home: string;
  now: number;
  onSelect: () => void;
  onOpen: () => void;
  onClose: () => void;
}) {
  const bg = bgJobChips(task);
  const word = cardStatusWord(task);
  const text = visibleReasons(reasons, task).join(" · ");
  const clear = reasons.length === 0;
  const hasPrs = prRefsOf(task).some((r) => r.repo);
  const hasBg = (task.background_jobs?.count ?? 0) > 0;
  const preview = messagePreview(task);
  const showChip = showProject && task.project !== "";
  const hasContext = preview !== "" || showChip;
  const hasFacts = hasPrs || hasBg || ports.length > 0 || text !== "" || clear;
  const when = taskWhenFor(task, now);
  const branch = pickBranch(
    holdRows,
    prRefsOf(task).filter((r) => r.repo).map((r) => peekPrHead(prKey(r))),
  );
  const hasFacts2 = hasFacts || branch !== null;
  return (
    <li
      className={
        "ws-card" + (selected ? " is-selected" : "") + (peeked ? ` ${PEEK_OPEN_CLASS}` : "")
      }
      data-key={task.key}
      data-status={task.status}
      data-needs-you={needsYou(task) ? "1" : undefined}
      data-unread={task.unread > 0 ? "1" : undefined}
      aria-selected={selected}
      {...(peekOn ? peekItemProps(task.key, peekOpenable(task)) : {})}
      onClick={() => {
        onSelect();
        onOpen();
      }}
    >
      <div className="ws-card-head">
        <StatusIcon
          status={taskColumn(task)}
          failed={ringFailed(task)}
          unread={task.unread > 0}
          count={task.unread}
          background={liveJobRow(task)}
        />
        <IdChip id={task.task_id} kind="task" />
        <span className={"ws-title" + (task.unread > 0 ? " is-unread" : "")}>{task.title || "(untitled)"}</span>
        {word && (
          <span className="ws-status" data-status={task.status}>
            {word}
          </span>
        )}
        {when.kind !== "none" && (
          <span className="ws-time" title={when.title}>
            {when.text}
          </span>
        )}
        <span className="task-card-doors ws-doors">
          <button
            type="button"
            className="task-card-door ws-close"
            aria-label={`Close ${task.task_id || task.title}`}
            title="Close (x)"
            onClick={(e) => {
              e.stopPropagation();
              onClose();
            }}
          >
            ×
          </button>
        </span>
      </div>
      {hasContext && (
        <div className="ws-card-ctx">
          {showChip && <IdentityChip name={basename(task.project)} title={tildePath(task.project, home)} />}
          {preview && <span className="ws-preview">{preview}</span>}
        </div>
      )}
      {hasFacts2 && (
        <div className="ws-card-facts">
          {branch && (
            <span className="ws-branch" title={branch.more > 0 ? `${branch.name} (+${branch.more} more)` : branch.name}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <line x1="6" y1="3" x2="6" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" />
              </svg>
              <span className="ws-branch-name">{branch.name}</span>
              {branch.more > 0 && <span className="ws-branch-more">+{branch.more}</span>}
            </span>
          )}
          <PrChips task={task} states={states} />
          {hasBg && (
            <span className="ws-bgs" title="Background jobs still running">
              {bg.titles.map((t, i) => (
                <span key={i} className="ws-bg">
                  ⧗ {t}
                </span>
              ))}
              {bg.more > 0 && <span className="ws-bg">{bg.titles.length ? `+${bg.more} bg` : `⧗ ${bg.more} bg`}</span>}
            </span>
          )}
          {ports.map((p) => (
            <a
              key={p}
              className="ws-port"
              href={`http://localhost:${p}`}
              target="_blank"
              rel="noopener noreferrer"
              title={`Open http://localhost:${p}`}
              onClick={(e) => e.stopPropagation()}
            >
              :{p}
            </a>
          ))}
          {text && (
            <span className="ws-marker" data-unknown={reasons.includes("unknown") ? "1" : undefined}>
              {text}
            </span>
          )}
          {clear && <span className="ws-clear">clear</span>}
        </div>
      )}
    </li>
  );
}

export function TaskWorkspace({
  tasks,
  allTasks,
  emptyLabel,
  peekOn,
  onReload,
  onNewTask,
  home = "",
}: {
  home?: string;
  /** The page's filtered set (Search and Project still narrow it). */
  tasks: Task[];
  /** Every task, archived included: the palette's corpus. */
  allTasks: Task[];
  emptyLabel: string;
  peekOn: boolean;
  onReload: () => void;
  onNewTask: () => void;
}) {
  const peekedKey = usePeekedKey();
  const [selected, setSelected] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const [undo, setUndo] = useState<{ key: string; title: string } | null>(null);
  const undoTimer = useRef<number | null>(null);

  const rows = useMemo(() => workspaceTasks(tasks).filter((t) => !hidden.has(t.key)), [tasks, hidden]);
  const keys = useMemo(() => rows.map((t) => t.key), [rows]);
  const { ports, holds, covered, failed } = useRowFacts(keys);
  const prKeys = useMemo(
    () => rows.flatMap((t) => prRefsOf(t).filter((r) => r.repo).map(prKey)),
    [rows],
  );
  const prStates = usePrStatuses(prKeys);
  const now = useNow();
  const showProject = useMemo(() => spansProjects(rows), [rows]);

  // What the key handler reads, kept current without re-binding it.
  const live = useRef({ rows, selected, peekedKey, searching });
  live.current = { rows, selected, peekedKey, searching };

  useEffect(
    () => () => {
      if (undoTimer.current !== null) window.clearTimeout(undoTimer.current);
    },
    [],
  );
  // A close is hidden at once, and forgotten once the data agrees (the task now
  // reads archived, or is gone), so a task that comes back (a reply, an Undo
  // from elsewhere) is not held hidden for ever.
  useEffect(() => {
    setHidden((h) => {
      if (h.size === 0) return h;
      const live = new Set(tasks.filter((t) => t.status !== "archived").map((t) => t.key));
      const keep = [...h].filter((k) => live.has(k));
      return keep.length === h.size ? h : new Set(keep);
    });
  }, [tasks]);

  const open = useCallback((task: Task): void => {
    if (!peekOpenable(task)) return;
    if (peekOn && openPeek(task.key)) return;
    const intent = openThreadIntent(task);
    if (intent) navigateUrl(intent.href);
  }, [peekOn]);

  const close = useCallback(
    (task: Task): void => {
      const { rows: now, peekedKey: peeked } = live.current;
      const at = now.findIndex((t) => t.key === task.key);
      const after = now[at + 1] ?? now[at - 1] ?? null;
      setHidden((h) => new Set([...h, task.key]));
      setSelected(after ? after.key : null);
      if (peeked === task.key) closePeek();
      archiveTask(task.key)
        .then(() => {
          setUndo({ key: task.key, title: task.title });
          if (undoTimer.current !== null) window.clearTimeout(undoTimer.current);
          undoTimer.current = window.setTimeout(() => setUndo(null), UNDO_MS);
          onReload();
        })
        .catch((e: Error) => {
          setHidden((h) => {
            const n = new Set(h);
            n.delete(task.key);
            return n;
          });
          notify({ title: e.message || "Could not close the task", tone: "error" });
        });
    },
    [onReload],
  );

  const reopen = useCallback(
    (key: string, thenOpen: boolean): void => {
      unarchiveTask(key)
        .then(() => {
          setHidden((h) => {
            const n = new Set(h);
            n.delete(key);
            return n;
          });
          setUndo((u) => (u && u.key === key ? null : u));
          onReload();
          if (thenOpen) {
            setSelected(key);
            if (peekOn) openPeek(key);
          }
        })
        .catch((e: Error) => notify({ title: e.message || "Could not reopen the task", tone: "error" }));
    },
    [onReload, peekOn],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.defaultPrevented) return;
      const st = live.current;
      const modal =
        typeof document !== "undefined" &&
        document.querySelector("[aria-modal='true'], dialog[open]") !== null;
      const action = workspaceKeyAction(e, {
        typing: isTyping(e.target),
        paletteOpen: st.searching || modal,
      });
      if (!action) return;
      const sel = st.rows.find((t) => t.key === st.selected) ?? null;
      switch (action) {
        case "next":
        case "prev": {
          e.preventDefault();
          const to = moveSelection(st.rows, st.selected, action === "next" ? 1 : -1);
          setSelected(to);
          if (to && st.peekedKey !== null && peekOn) openPeek(to);
          return;
        }
        case "open":
          if (sel) {
            e.preventDefault();
            open(sel);
          }
          return;
        case "close":
          if (sel) {
            e.preventDefault();
            close(sel);
          }
          return;
        case "new":
          e.preventDefault();
          onNewTask();
          return;
        case "search":
          e.preventDefault();
          setSearching(true);
          return;
        case "needs": {
          const to = nextNeedingYou(st.rows, st.selected);
          if (to) {
            e.preventDefault();
            setSelected(to);
          }
          return;
        }
        case "escape":
          // The peek closes itself on this key first; only a press with no
          // peek up clears the selection.
          if (st.peekedKey === null && st.selected !== null) {
            e.preventDefault();
            setSelected(null);
          }
          return;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close, onNewTask, peekOn]);

  useEffect(() => {
    if (!selected) return;
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(".ws-card"))) {
      if (el.dataset.key === selected) el.scrollIntoView?.({ block: "nearest" });
    }
  }, [selected]);

  return (
    <div className="ws-root" data-workspace>
      {rows.length === 0 ? (
        <p className="schedule-tv-empty">{emptyLabel}</p>
      ) : (
        <ul className="ws-list" role="listbox" aria-label="Open tasks">
          {rows.map((t) => {
            const covers = covered.has(t.key);
            const reasons = holdReasons(t, {
              holds,
              holdsReady: covers,
              holdsFailed: failed,
              prStates: prStates,
            });
            return (
              <WorkspaceCard
                key={t.key}
                task={t}
                selected={selected === t.key}
                peeked={peekedKey === t.key}
                peekOn={peekOn}
                ports={ports[t.key] ?? []}
                reasons={reasons}
                holdRows={holds[t.key] ?? []}
                states={prStates}
                showProject={showProject}
                home={home}
                now={now}
                onSelect={() => setSelected(t.key)}
                onOpen={() => open(t)}
                onClose={() => close(t)}
              />
            );
          })}
        </ul>
      )}
      {undo && (
        <div className="ws-undo" role="status">
          <span className="ws-undo-text">Closed “{undo.title}”</span>
          <button type="button" className="ws-undo-btn" onClick={() => reopen(undo.key, false)}>
            Undo
          </button>
        </div>
      )}
      {searching && (
        <WorkspaceSearch
          tasks={allTasks}
          onClose={() => setSearching(false)}
          onPick={(t) => {
            setSearching(false);
            if (t.status === "archived") reopen(t.key, true);
            else {
              setSelected(t.key);
              open(t);
            }
          }}
        />
      )}
    </div>
  );
}
