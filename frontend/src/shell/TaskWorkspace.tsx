// The Workspace view: every task that is not closed, newest first, each one a
// card saying what it is doing and what keeps it open (workspace-lib.ts), with a
// close button, an Undo toast and a keyboard map. Its rules live in
// workspace-lib; this file draws them and runs the two polls the rows need
// (ports and holds), asked for the visible rows only.
import { isMod } from "@platform/lib/platform";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { PrPopList } from "@platform/ui/TaskPrChip";
import { WorkspaceSearch } from "@shell/WorkspaceSearch";
import { useArchiveConfirm } from "@shell/archive-confirm";
import { selectableTask } from "@shell/task-selection";
import { closedToastText } from "@shell/teardown-notice";
import { BulkBar, GroupCheck, RowCheck, useBulkArchive, type BulkSelect } from "@shell/TaskBulk";
import { closePeek, openPeek, peekItemProps, PEEK_OPEN_CLASS, usePeekedKey } from "@shell/task-peek-store";
import { useNow } from "@platform/lib/clock";
import { ICON_ARCHIVE, ICON_UNARCHIVE, IdChip, IdentityChip, PrNumChip, StatusIcon } from "@shell/ScheduleTaskViews";
import {
  type FilingIntent,
  basename,
  filingIntent,
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
  bgJobBadge,
  capList,
  groupCollapsed,
  groupTasks,
  MAX_PRS,
  orderPrs,
  messagePreview,
  blockingReasons,
  holdReasons,
  moveSelection,
  needsYou,
  nextNeedingYou,
  pickBranch,
  portBadges,
  visibleGroupTasks,
  visibleReasons,
  workspaceKeyAction,
  workspaceTasks,
  type WorkspaceFacts,
  type WorkspaceGroupKey,
  DIRTY_REASON,
  dirtyBadge,
  dirtyTitle,
} from "@shell/workspace-lib";

/** How long the Undo toast stays up after a close. */
export const UNDO_MS = 8000;
const POLL_MS = 15000;
/** The server answers at most this many ids per read. */
const MAX_IDS = 100;
const COLLAPSED_KEY = "fused.tasks.workspace.collapsed";

/** Group headers the reader folded (true) or unfolded (false); absent = default. */
function readCollapsed(): Record<string, boolean> {
  try {
    const v: unknown = JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? "{}");
    if (v && typeof v === "object" && !Array.isArray(v)) {
      return Object.fromEntries(Object.entries(v).filter(([, b]) => typeof b === "boolean")) as Record<string, boolean>;
    }
  } catch {
    // no storage, or not JSON
  }
  return {};
}

function writeCollapsed(v: Record<string, boolean>): void {
  try {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify(v));
  } catch {
    // no storage
  }
}

function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== "string") return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable === true;
}

/** `keys` in runs of at most `size`, the server's per-read cap. */
function chunked(keys: string[], size: number): string[][] {
  const out: string[][] = [];
  for (let i = 0; i < keys.length; i += size) out.push(keys.slice(i, i + size));
  return out;
}

/** Holds for EVERY key, read in chunks of MAX_IDS one after another and merged
 *  as each lands. A chunk that fails marks only its own keys failed. */
function useRowHolds(keys: string[]): {
  holds: Record<string, TaskHold[]>;
  covered: ReadonlySet<string>;
  failed: ReadonlySet<string>;
} {
  const [holds, setHolds] = useState<Record<string, TaskHold[]>>({});
  const [covered, setCovered] = useState<ReadonlySet<string>>(new Set());
  const [failed, setFailed] = useState<ReadonlySet<string>>(new Set());
  const joined = keys.join("\n");
  useEffect(() => {
    const ids = joined ? joined.split("\n") : [];
    if (ids.length === 0) return;
    let dead = false;
    let busy = false;
    const tick = async (): Promise<void> => {
      if (busy || (typeof document !== "undefined" && document.visibilityState === "hidden")) return;
      busy = true;
      try {
        for (const chunk of chunked(ids, MAX_IDS)) {
          if (dead) return;
          try {
            const h = await fetchTaskHolds(chunk);
            if (dead) return;
            setHolds((prev) => {
              const next = { ...prev };
              for (const k of chunk) delete next[k];
              return Object.assign(next, h);
            });
            setCovered((prev) => new Set([...prev, ...chunk]));
            setFailed((prev) => (chunk.some((k) => prev.has(k)) ? new Set([...prev].filter((k) => !chunk.includes(k))) : prev));
          } catch {
            if (!dead) setFailed((prev) => new Set([...prev, ...chunk]));
          }
        }
      } finally {
        busy = false;
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), POLL_MS);
    return () => {
      dead = true;
      window.clearInterval(id);
    };
  }, [joined]);
  return { holds, covered, failed };
}

/** Ports for the cards on screen only (at most MAX_IDS), on a timer. */
function useRowPorts(keys: string[]): Record<string, number[]> {
  const [ports, setPorts] = useState<Record<string, number[]>>({});
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
    };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => {
      dead = true;
      window.clearInterval(id);
    };
  }, [joined]);
  return ports;
}

function PrChips({ task, states }: { task: Task; states: ReturnType<typeof usePrStatuses> }) {
  const refs = prRefsOf(task).filter((r) => r.repo);
  if (refs.length === 0) return null;
  const { shown, more } = capList(orderPrs(refs, states), MAX_PRS);
  const rest = orderPrs(refs, states).slice(MAX_PRS);
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
      {more > 0 && (
        // The chat header's overflow popover (platform/ui/TaskPrChip): the rest
        // as links with their GitHub status. Clicks stop here so they never
        // reach the card's open/peek handler (the popover renders in a portal,
        // but React still bubbles its events through this tree).
        <Popover>
          <PopoverTrigger
            render={
              <button
                type="button"
                className="task-pr-more"
                aria-label={`${more} more pull requests`}
                onClick={(e) => e.stopPropagation()}
              />
            }
          >
            +{more}
          </PopoverTrigger>
          <PopoverContent className="task-pr-pop" align="start" onClick={(e) => e.stopPropagation()}>
            <PrPopList refs={rest} />
          </PopoverContent>
        </Popover>
      )}
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
  onFile,
  bulk,
}: {
  task: Task;
  bulk: BulkSelect;
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
  /** The ring-slot button: archive (with Undo) or unarchive, the `x` key's own path. */
  onFile: (intent: FilingIntent) => void;
}) {
  const file = filingIntent(task);
  const bg = bgJobBadge(task);
  // The worktree counts collapse to one word badge (dirtyBadge); the exact counts
  // ride in its tooltip. "unknown", "checking" and the rest stay as text.
  const reasonList = visibleReasons(reasons);
  const dirty = dirtyBadge(reasonList);
  // "checking" renders as a shimmer pill in the badge slot, never as text.
  const checking = reasonList.includes("checking");
  const text = reasonList.filter((r) => !DIRTY_REASON.test(r) && r !== "checking").join(" · ");
  const clear = reasons.length === 0;
  const hasPrs = prRefsOf(task).some((r) => r.repo);
  const hasBg = bg !== null;
  const preview = messagePreview(task);
  const showChip = showProject && task.project !== "";
  const hasContext = preview !== "" || showChip;
  const when = taskWhenFor(task, now);
  const branch = pickBranch(
    holdRows,
    prRefsOf(task).filter((r) => r.repo).map((r) => peekPrHead(prKey(r))),
  );
  // Line 2 exists for a preview/project chip or for real facts. "clear" alone
  // never opens it: it rides line 1's right edge instead.
  const realFacts = hasPrs || hasBg || ports.length > 0 || dirty !== null || text !== "" || checking || branch !== null;
  const hasLine2 = hasContext || realFacts;
  const clearTag = clear ? <span className="ws-badge ws-clear" title="Nothing holds this task open, so it is safe to archive">safe to archive</span> : null;
  return (
    <li
      className={
        "ws-card" + (selected ? " is-selected" : "") + (peeked ? ` ${PEEK_OPEN_CLASS}` : "")
      }
      data-key={task.key}
      data-checked={bulk.selection.has(task.key) ? "1" : undefined}
      data-selecting={bulk.selection.size > 0 ? "1" : undefined}
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
        {/* The selection checkbox lives in the left gutter (task-workspace.css),
            out of flow, shown on hover, focus or while anything is selected. */}
        <RowCheck select={bulk} task={task} className="ws-check" />
        {/* THE MARK SLOT, same as the List row's: the ring at rest, the
            Archive (or Unarchive) button in the same box on hover or keyboard
            focus. Classes and filingIntent are the List's; the press goes
            through the Workspace's own archive path (Undo toast, rollback). */}
        <span className="tasks-rowmark ws-mark">
          <StatusIcon
            status={taskColumn(task)}
            failed={ringFailed(task)}
            unread={task.unread > 0}
            count={task.unread}
            background={liveJobRow(task)}
          />
          {file && (
            <button
              type="button"
              className={"tasks-act tasks-act--" + file.kind}
              data-hint={file.title}
              aria-label={file.label}
              onClick={(e) => {
                e.stopPropagation();
                onFile(file);
              }}
            >
              {file.kind === "archive" ? ICON_ARCHIVE : ICON_UNARCHIVE}
            </button>
          )}
        </span>
        <IdChip id={task.task_id} kind="task" />
        <span className={"ws-title" + (task.unread > 0 ? " is-unread" : "")}>{task.title || "(untitled)"}</span>
        {when.kind !== "none" && (
          <span className="ws-time" title={when.title}>
            {when.text}
          </span>
        )}
        {!hasLine2 && clearTag}
      </div>
      {hasLine2 && (
        <div className="ws-card-line2">
      {hasContext && (
        <div className="ws-card-ctx">
          {showChip && <IdentityChip name={basename(task.project)} title={tildePath(task.project, home)} />}
          {preview && <span className="ws-preview">{preview}</span>}
        </div>
      )}
      {(realFacts || clear) && (
        <div className="ws-card-facts" aria-busy={checking ? "true" : undefined}>
          {branch && (
            <span className="ws-badge ws-branch" title={branch.also.length > 0 ? `${branch.name}\nalso: ${branch.also.join(", ")}` : branch.name}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <line x1="6" y1="3" x2="6" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" />
              </svg>
              <span className="ws-branch-name">{branch.name}</span>
            </span>
          )}
          <PrChips task={task} states={states} />
          {bg && (
            <span className="ws-badge ws-bg" title={bg.title}>
              {bg.label}
            </span>
          )}
          {portBadges(ports).map((b) =>
            b.port !== undefined ? (
              <a
                key={b.label}
                className="ws-badge ws-port"
                href={`http://localhost:${b.port}`}
                target="_blank"
                rel="noopener noreferrer"
                title={b.title}
                onClick={(e) => e.stopPropagation()}
              >
                {b.label}
              </a>
            ) : (
              <span key={b.label} className="ws-badge ws-port" title={b.title}>
                {b.label}
              </span>
            ),
          )}
          {dirty && (
            <span className="ws-badge ws-marker ws-dirty" title={dirtyTitle(reasonList)}>
              {dirty}
            </span>
          )}
          {text && (
            <span className="ws-badge ws-marker" data-unknown={reasons.includes("unknown") ? "1" : undefined}>
              {text}
            </span>
          )}
          {checking && (
            <>
              <span className="skel-bar ws-skel" aria-hidden="true" />
              <span className="sr-only">Checking worktree</span>
            </>
          )}
          {clearTag}
        </div>
      )}
        </div>
      )}
    </li>
  );
}

export function TaskWorkspace({
  tasks: tasksIn,
  resetKey = "",
  allTasks,
  emptyLabel,
  peekOn,
  onReload,
  onNewTask,
  home = "",
}: {
  home?: string;
  /** Changes when the page's filters or search do: the selection empties. */
  resetKey?: string;
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
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(readCollapsed);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const [undo, setUndo] = useState<{ key: string; text: string } | null>(null);
  const undoTimer = useRef<number | null>(null);

  const { tasks, selection, archiveSelected, node: bulkNode, bulk } = useBulkArchive({
    tasks: tasksIn,
    selectable: selectableTask,
    resetKey,
    onReload,
  });
  const base = useMemo(() => workspaceTasks(tasks).filter((t) => !hidden.has(t.key)), [tasks, hidden]);
  const keys = useMemo(() => base.map((t) => t.key), [base]);
  const { holds, covered, failed } = useRowHolds(keys);
  const prKeys = useMemo(
    () => base.flatMap((t) => prRefsOf(t).filter((r) => r.repo).map(prKey)),
    [base],
  );
  const prStatesRaw = usePrStatuses(prKeys);
  // A new object every call: keep the identity until a status itself changes, so
  // factsFor and the grouping do not recompute on every render.
  const prSig = prKeys.map((k) => `${k}=${prStatesRaw[k] ?? ""}`).join("\n");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const prStates = useMemo(() => prStatesRaw, [prSig]);
  const factsFor = useCallback(
    (t: Task): WorkspaceFacts => ({ holds, holdsReady: covered.has(t.key), holdsFailed: failed.has(t.key), prStates }),
    [holds, covered, failed, prStates],
  );
  // Grouped by state; the flat list below is what j/k/n walk, so the keyboard
  // order is the order on screen. Hidden (just-closed) rows never count.
  const groups = useMemo(() => groupTasks(base, factsFor), [base, factsFor]);
  // Only the cards on screen: a folded group has none, a capped one its first 8.
  // j/k/n walk this list, so they never land on a card that is not drawn.
  const shownGroups = useMemo(
    () =>
      groups.map((g) => {
        const { shown, more } = visibleGroupTasks(g.tasks, expanded.has(g.key));
        const folded = groupCollapsed(g.key, collapsed);
        return { ...g, folded, shown: folded ? [] : shown, more: folded ? 0 : more };
      }),
    [groups, expanded, collapsed],
  );
  const rows = useMemo(() => shownGroups.flatMap((g) => g.shown), [shownGroups]);
  const ports = useRowPorts(useMemo(() => rows.map((t) => t.key), [rows]));
  const now = useNow();
  const showProject = useMemo(() => spansProjects(rows), [rows]);

  // What the key handler reads, kept current without re-binding it.
  const live = useRef({ rows, groups, collapsed, selected, peekedKey, searching, selection, archiveSelected });
  live.current = { rows, groups, collapsed, selected, peekedKey, searching, selection, archiveSelected };

  const toggleGroup = useCallback((key: WorkspaceGroupKey): void => {
    const next = { ...live.current.collapsed, [key]: !groupCollapsed(key, live.current.collapsed) };
    writeCollapsed(next);
    setCollapsed(next);
  }, []);

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

  const archiveAsk = useArchiveConfirm();
  const doClose = useCallback(
    (task: Task): void => {
      const { rows: now, peekedKey: peeked } = live.current;
      const at = now.findIndex((t) => t.key === task.key);
      const after = now[at + 1] ?? now[at - 1] ?? null;
      setHidden((h) => new Set([...h, task.key]));
      setSelected(after ? after.key : null);
      if (peeked === task.key) closePeek();
      archiveTask(task.key)
        .then((res) => {
          setUndo({ key: task.key, text: closedToastText(`Closed \u201c${task.title}\u201d`, [res.teardown]) });
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
  // The `x` key and the ring-slot button both land here: a running task asks first.
  const close = useCallback(
    (task: Task): void => archiveAsk.guard(task, () => doClose(task)),
    [archiveAsk.guard, doClose],
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
      // Keys inside the PR overflow popover (a portal) are the popover's own:
      // Space/Enter on a link must not select or archive the card behind it.
      if ((e.target as HTMLElement | null)?.closest?.(".task-pr-pop")) return;
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
          // A focused button or link keeps its own Enter.
          if (sel && !/^(BUTTON|A)$/.test((e.target as HTMLElement | null)?.tagName ?? "")) {
            e.preventDefault();
            open(sel);
          }
          return;
        case "check":
          // A focused button or link keeps its own space.
          if (sel && selectableTask(sel) && !/^(BUTTON|A)$/.test((e.target as HTMLElement | null)?.tagName ?? "")) {
            e.preventDefault();
            st.selection.toggle(sel.key);
          }
          return;
        case "close":
          if (st.selection.size > 0) {
            e.preventDefault();
            st.archiveSelected();
          } else if (sel) {
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
          let to = nextNeedingYou(st.rows, st.selected);
          if (!to) {
            // Every drawn card is checked first; a "Needs you" group that is
            // folded or capped may hold one that is not drawn.
            const g = st.groups.find((x) => x.key === "needs");
            if (g && g.tasks.length > 0) {
              to = g.tasks[0].key;
              if (groupCollapsed("needs", st.collapsed)) {
                const next = { ...st.collapsed, needs: false };
                writeCollapsed(next);
                setCollapsed(next);
              }
              setExpanded((x) => new Set([...x, "needs"]));
            }
          }
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

  // Mod+K is App's cheat-sheet chord too, and App's document listener preventDefaults
  // it before the window handler above could act. This capture listener runs first
  // and stops the event, so the Workspace search palette is the one owner here.
  useEffect(() => {
    const onChord = (e: KeyboardEvent): void => {
      if (e.isComposing || e.defaultPrevented || !isMod(e) || e.altKey || e.key.toLowerCase() !== "k") return;
      if (document.querySelector("[aria-modal='true'], dialog[open]") !== null) return;
      e.preventDefault();
      e.stopPropagation();
      setSearching(true);
    };
    window.addEventListener("keydown", onChord, true);
    return () => window.removeEventListener("keydown", onChord, true);
  }, []);

  useEffect(() => {
    if (!selected) return;
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(".ws-card"))) {
      if (el.dataset.key === selected) el.scrollIntoView?.({ block: "nearest" });
    }
  }, [selected]);

  return (
    <div className="ws-root" data-workspace>
      <BulkBar count={selection.size} onArchive={archiveSelected} onClear={selection.clear} />
      {groups.length === 0 ? (
        <p className="schedule-tv-empty">{emptyLabel}</p>
      ) : (
        <ul className="ws-list" role="listbox" aria-label="Open tasks">
          {shownGroups.map((g, gi) => (
            <Fragment key={g.key}>
              <li
                role="presentation"
                className="tasks-group-head ws-group-head"
                data-group={g.key}
                data-first={gi === 0 ? "1" : undefined}
                data-collapsed={g.folded ? "1" : "0"}
                data-selecting={bulk.selection.size > 0 ? "1" : undefined}
              >
                <GroupCheck select={bulk} keys={g.tasks.filter(selectableTask).map((t) => t.key)} name={g.label} />
                <button type="button" className="tasks-group-toggle ws-group-toggle" aria-expanded={!g.folded} onClick={() => toggleGroup(g.key)}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <path d="m9 18 6-6-6-6" />
                  </svg>
                  <span className="tasks-group-name">{g.label}</span>
                  <span className="tasks-group-count">{g.tasks.length}</span>
                </button>
              </li>
              {g.shown.map((t) => {
                const reasons = blockingReasons(holdReasons(t, factsFor(t)));
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
                    onFile={(f) => (f.kind === "archive" ? close(t) : reopen(t.key, false))}
                    bulk={bulk}
                  />
                );
              })}
              {g.more > 0 && (
                <li role="presentation" className="ws-more-row">
                  <button
                    type="button"
                    className="schedule-tv-more ws-more"
                    onClick={() => setExpanded((x) => new Set([...x, g.key]))}
                  >
                    Show {g.more} more
                  </button>
                </li>
              )}
            </Fragment>
          ))}
        </ul>
      )}
      {undo && (
        <div className="ws-undo" role="status">
          <span className="ws-undo-text">{undo.text}</span>
          <button type="button" className="ws-undo-btn" onClick={() => reopen(undo.key, false)}>
            Undo
          </button>
        </div>
      )}
      {bulkNode}
      {archiveAsk.node}
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
