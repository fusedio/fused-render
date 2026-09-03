// The Tasks page's two task views — the List (an accordion per task) and the
// Board (columns of task cards).
//
// The page no longer MERGES two feeds: `/api/tasks` returns the model, already
// merged, titled, counted and sorted newest-first. These components render it
// and decide nothing — every decision they need is a tasks-lib function.
//
//   TASK-002   one Claude session, one thread
//   ├─ MSG-003  newest first
//   ├─ MSG-002
//   └─ MSG-001
//
// Look and feel: Tailwind utilities on the Flow design language (the flow
// composites in platform/ui/flow, the page's own marks in shell/tasks-ui.tsx).
// The bare `tasks-*` / `schedule-*` class names that survive on a handful of
// elements are UNSTYLED hooks the Tasks tour (platform/lib/tours/tasks.ts)
// anchors to; nothing here is drawn by a stylesheet of its own.
import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { DragEvent as ReactDragEvent } from "react";
import {
  Archive,
  ArchiveRestore,
  Ban,
  CheckCheck,
  ChevronRight,
  ExternalLink,
  FileText,
  Folder,
  MessageSquare,
  Pencil,
  Play,
  RotateCcw,
  Search,
  SkipForward,
} from "lucide-react";
import {
  cancelScheduledMessage,
  getTaskMessages,
  markTaskMessageRead,
  markWholeTaskRead,
  resendScheduledMessage,
  runScheduledNow,
  archiveTask,
  unarchiveTask,
} from "@platform/lib/api";
import type { Task, TaskMessage } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { EntityList } from "@platform/ui/flow/EntityRow";
import { SectionHeading, Tiny } from "@platform/ui/flow/Typography";
import { Empty, EmptyDescription, EmptyHeader } from "@platform/shadcn/ui/empty";
import { BOARD_COLUMNS, columnLabel } from "./schedule-lib";
import type { BoardColumn } from "./schedule-lib";
import {
  EMPTY_FILTERS,
  EMPTY_LIST_MEMORY,
  LANE_CHOICE_KEY,
  LIST_MEMORY_KEY,
  basename,
  cancelIntent,
  carryMarkToHeld,
  dropAction,
  dropLanes,
  filingIntent,
  filterTasks,
  filtersForView,
  firstLine,
  groupByColumn,
  heldMessages,
  isDraggable,
  isExpandable,
  isFailedTask,
  isUpcomingTask,
  laneRolledUp,
  laneUnread,
  sortByLane,
  showsRowActions,
  statusColumn,
  markAllRead,
  markRead,
  markReadIntent,
  messageEditEntry,
  messageHref,
  taskFile,
  threadTone,
  messageWhenTitle,
  nextRunChip,
  outcomeTag,
  openMessageHref,
  openThreadIntent,
  opensElsewhere,
  parseLaneChoices,
  parseListMemory,
  projectOptions,
  relativeWhen,
  settleMarkAllRead,
  spansProjects,
  taskColumn,
  taskRunIntent,
  taskUnread,
  taskUnreadLabel,
  taskWhen,
  threadView,
  tildePath,
  toggleExpanded,
  unmarkAllRead,
  unmarkRead,
  unreadMarker,
  upcomingEditEntry,
} from "./tasks-lib";
import type {
  FilingIntent,
  LaneChoices,
  ListMemory,
  OpenThreadIntent,
  TaskFilters,
  TaskRunIntent,
} from "./tasks-lib";
import {
  CardFrame,
  FilterItem,
  FilterMenu,
  FolderChip,
  IdChip,
  Note,
  OutcomePill,
  RowAction,
  RowFrame,
  RowLink,
  StatusIcon,
} from "./tasks-ui";

// The page composes these from one import; re-exported so Scheduled.tsx takes
// its filter type, empty value and filter function from the module it takes
// the views from.
export { EMPTY_FILTERS, filterTasks, filtersForView, projectOptions, tildePath, basename };
export type { TaskFilters };
export { StatusIcon, FolderChip as IdentityChip } from "./tasks-ui";

/**
 * Whether ANY hover-revealed action is drawn on this page — OFF at Akshil's
 * request (2026-08-17): the List task row's Mark read / Run now / Open chat,
 * the List message row's Edit / Cancel, the Board card's Run now. ARCHIVE is
 * not behind it (2026-08-18: brought back, visible on hover) on both views at
 * once. Everything behind the flag is still built and decided by tasks-lib;
 * only the RENDER is gated, and not rendered rather than hidden — an invisible
 * button is still a tab stop. Annotated `boolean` so a flip is a value change,
 * not a type change.
 */
const SHOW_ROW_ACTIONS: boolean = false;

const ICON = "size-3.5";

// ---- read bookkeeping --------------------------------------------------------
// Clicking a message marks it read, and the dot has to go NOW: the page polls
// every 20s. The write goes to the server AND into a local set merged over the
// next poll's answer until the server catches up. Never pruned.

function useReadSet() {
  const [read, setRead] = useState<Set<string>>(() => new Set());
  const clear = (taskKey: string, m: TaskMessage) => {
    if (!m.unread) return;
    setRead((cur) => markRead(cur, taskKey, m.message_id));
    // Fire and forget as far as the NAVIGATION goes, but the mark is taken back
    // on a refusal (tasks-lib.unmarkRead): the local entry outranks the poll
    // for as long as this component lives.
    void markTaskMessageRead(taskKey, m.message_id).catch(() => {
      setRead((cur) => unmarkRead(cur, taskKey, m.message_id));
    });
  };
  // The whole task: a concrete id for every message HELD plus one sentinel for
  // the ones outside the window (tasks-lib.markAllRead).
  const clearAll = (task: Task, held?: TaskMessage[]) => {
    setRead((cur) => markAllRead(cur, task, held));
  };
  // A thread that has only just ARRIVED under a mark still standing adopts it.
  const carryAll = (task: Task, held: TaskMessage[]) => {
    setRead((cur) => carryMarkToHeld(cur, task, held));
  };
  const restoreAll = (taskKey: string, held: TaskMessage[]) => {
    setRead((cur) => unmarkAllRead(cur, taskKey, held));
  };
  const settleAll = (
    taskKey: string,
    held: TaskMessage[],
    answer: { unread: number },
  ) => {
    setRead((cur) => settleMarkAllRead(cur, taskKey, held, answer));
  };
  return { read, clear, clearAll, carryAll, restoreAll, settleAll };
}

interface ReadMarks {
  clearAll: (task: Task, held?: TaskMessage[]) => void;
  restoreAll: (taskKey: string, held: TaskMessage[]) => void;
  settleAll: (
    taskKey: string,
    held: TaskMessage[],
    answer: { unread: number },
  ) => void;
}

// ---- the run, for both views -------------------------------------------------

/** Spend a TaskRunIntent: the ONE place either view turns `kind` into a call.
 *  Returns the sentence to show ("" for nothing); refusals THROW. */
async function performRun(intent: TaskRunIntent): Promise<string> {
  if (intent.kind === "resend") {
    const res = await resendScheduledMessage(intent.entryId);
    return res.note ?? "";
  }
  await runScheduledNow(intent.entryId);
  return "";
}

/** Take one task back out of Archive and say where it landed — the server
 *  derives the lane, so the card is about to appear somewhere the reader did
 *  not choose. Refusals THROW. */
async function performUnarchive(key: string): Promise<string> {
  const said = await unarchiveTask(key);
  if (!said.unfiled) return `Nothing to unarchive — still ${columnLabel(statusColumn(said.status))}.`;
  return `Unarchived — back in ${columnLabel(statusColumn(said.status))}.`;
}

/** Spend an OpenThreadIntent: the ONE place either view opens a conversation.
 *  Local clear first, one whole-task request, navigation never waits on it;
 *  a refusal or a non-zero remaining count corrects the mark afterwards. */
function performOpen(
  task: Task,
  intent: OpenThreadIntent,
  marks: ReadMarks,
  held: TaskMessage[],
): void {
  if (intent.markRead) {
    marks.clearAll(task, held);
    void markWholeTaskRead(task.key)
      .then((answer) => marks.settleAll(task.key, held, answer))
      .catch(() => marks.restoreAll(task.key, held));
  }
  navigateUrl(intent.href);
}

// ---- toolbar: search + status + project --------------------------------------

export function TaskFilterControls({
  filters,
  projects,
  home = "",
  onChange,
  hideArchiveStatus = false,
}: {
  filters: TaskFilters;
  /** Every folder that has a task — `projectOptions(tasks)`. */
  projects: string[];
  home?: string;
  onChange: (next: TaskFilters) => void;
  /** True while the Calendar is up: it draws nothing for an archived task, so
   *  the Archive row and its count are hidden there. The STORED value is
   *  untouched (tasks-lib.filtersForView). */
  hideArchiveStatus?: boolean;
}) {
  const toggleStatus = (key: BoardColumn) =>
    onChange({
      ...filters,
      statuses: filters.statuses.includes(key)
        ? filters.statuses.filter((s) => s !== key)
        : [...filters.statuses, key],
    });

  const statusColumns = hideArchiveStatus
    ? BOARD_COLUMNS.filter((c) => c.key !== "archived")
    : BOARD_COLUMNS;
  const statusCount = hideArchiveStatus
    ? filters.statuses.filter((s) => s !== "archived").length
    : filters.statuses.length;

  const toggleProject = (path: string) =>
    onChange({
      ...filters,
      projects: filters.projects.includes(path)
        ? filters.projects.filter((p) => p !== path)
        : [...filters.projects, path],
    });

  return (
    <div className="flex items-center gap-2 min-w-0">
      <div className="relative w-56 max-w-full">
        <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input
          type="search"
          className="h-7 pl-7 text-[0.8rem] md:text-[0.8rem]"
          value={filters.search}
          placeholder="Search tasks…"
          aria-label="Search tasks"
          onChange={(e) => onChange({ ...filters, search: e.target.value })}
          onKeyDown={(e) => {
            if (e.key === "Escape") e.currentTarget.blur();
          }}
        />
      </div>

      <FilterMenu
        label="Status"
        count={statusCount}
        icon={<StatusIcon status="upcoming" label="Status" className="border-muted-foreground" />}
        onClear={() => onChange({ ...filters, statuses: [] })}
      >
        {() =>
          statusColumns.map((col) => {
            const on = filters.statuses.includes(col.key);
            return (
              <FilterItem key={col.key} on={on} onClick={() => toggleStatus(col.key)}>
                <StatusIcon status={col.key} />
                <span>{col.label}</span>
              </FilterItem>
            );
          })
        }
      </FilterMenu>

      {/* Project is auto-detected from the tasks themselves, so the menu is
          absent on a machine whose tasks all live in one folder. */}
      {projects.length > 1 && (
        <FilterMenu
          label="Project"
          count={filters.projects.length}
          icon={<Folder className={ICON} aria-hidden />}
          onClear={() => onChange({ ...filters, projects: [] })}
        >
          {() =>
            projects.map((path) => {
              const on = filters.projects.includes(path);
              return (
                <FilterItem key={path} on={on} title={tildePath(path, home)} onClick={() => toggleProject(path)}>
                  <Folder className={cn(ICON, "shrink-0 text-muted-foreground")} aria-hidden />
                  <span className="truncate">{basename(path)}</span>
                </FilterItem>
              );
            })
          }
        </FilterMenu>
      )}
    </div>
  );
}

// ---- List view: one accordion per task ---------------------------------------

/** How long the list keeps trying to reach the offset it was left at. */
const RESTORE_WINDOW_MS = 3000;
/** Scroll fires per frame; the store is written once the reader pauses. */
const WRITE_DEBOUNCE_MS = 150;

/** Per-TAB, per-sitting (sessionStorage). A blocked store costs the memory,
 *  never the page. */
function readListMemory(): ListMemory {
  try {
    return parseListMemory(sessionStorage.getItem(LIST_MEMORY_KEY));
  } catch {
    return EMPTY_LIST_MEMORY;
  }
}

function writeListMemory(memory: ListMemory): void {
  try {
    sessionStorage.setItem(LIST_MEMORY_KEY, JSON.stringify(memory));
  } catch {
    // best-effort; a full or blocked store never breaks the list
  }
}

export function TaskList({
  tasks,
  home = "",
  stale = false,
  onEditEntry,
  onReload,
  onPickProject,
  pinnedProjects = [],
  emptyLabel = "Nothing to show here.",
}: {
  /** Already filtered, in the SERVER's order. Never re-sorted here. */
  tasks: Task[];
  home?: string;
  /** Is this empty list a FAILURE rather than an answer? A failed poll empties
   *  `tasks` exactly like a filter that matched nothing; the scroll memory
   *  must tell them apart. */
  stale?: boolean;
  /** Open the schedule form on a message that has not gone out yet. */
  onEditEntry?: (entryId: string) => void;
  /** Re-read the list after a cancel lands (or fails). Optional: the page
   *  polls anyway. */
  onReload?: () => void;
  /** Narrow the page to ONE project — a press on a row's folder chip. */
  onPickProject?: (project: string) => void;
  /** The projects the page is currently FILTERED to. */
  pinnedProjects?: string[];
  emptyLabel?: string;
}) {
  // Collapsed by default; the set holds what is OPEN, seeded from THIS TAB's
  // memory. Read once, into a ref: both halves of the memory are initial state.
  const memory = useRef<ListMemory>(readListMemory());
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set(memory.current.expanded),
  );
  // WHERE THE READER JUST WAS — lit the moment it is pressed.
  const [selected, setSelected] = useState(() => memory.current.selected);
  const select = (key: string) => {
    setSelected(key);
    remember({ ...memory.current, selected: key });
  };
  // Full threads fetched on expand, keyed by task. They REPLACE the three the
  // listing carried, so no message is ever drawn twice.
  const [loaded, setLoaded] = useState<Record<string, TaskMessage[]>>({});
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  // An unarchive's destination sentence, held by the PAGE: a status filter can
  // unmount the very row the sentence sits on.
  const [pageNote, setPageNote] = useState("");
  const { read, clear, clearAll, carryAll, restoreAll, settleAll } = useReadSet();

  // The latest poll's tasks, readable from ACROSS an await.
  const latest = useRef(tasks);
  useEffect(() => {
    latest.current = tasks;
  }, [tasks]);

  // Whether a row draws its folder chip: only when the rows span folders
  // (spansProjects) — or a project is pinned, so the control that narrowed the
  // list stays on screen wearing the state.
  const pinnedKey = pinnedProjects.join(" ");
  const showProject = useMemo(
    () => spansProjects(tasks) || pinnedKey !== "",
    [tasks, pinnedKey],
  );

  // Rank order, then printed time (tasks-lib.sortByLane). Memoised: this runs
  // on every keystroke of the search box.
  const rows = useMemo(() => sortByLane(tasks), [tasks]);

  /** Open or close a task — and, on the way OPEN, fetch the rest of its
   *  thread. Exactly one trip: only when opening, only when the listing is
   *  truncated, never while a fetch is in flight or already landed. */
  const toggle = (task: Task) => {
    const opening = !expanded.has(task.key);
    setExpanded((cur) => toggleExpanded(cur, task.key));
    if (opening && threadView(task).more && !loaded[task.key] && !loading[task.key]) {
      void showMore(task);
    }
  };

  const showMore = async (task: Task) => {
    setLoading((cur) => ({ ...cur, [task.key]: true }));
    setErrors((cur) => {
      const next = { ...cur };
      delete next[task.key];
      return next;
    });
    try {
      const r = await getTaskMessages(task.key);
      const thread = r.messages ?? [];
      setLoaded((cur) => ({ ...cur, [task.key]: thread }));
      // This reply is a READ of a value we may already have overridden (Mark
      // read a moment ago): carryAll adopts the standing mark onto it.
      const fresh = latest.current.find((t) => t.key === task.key) ?? task;
      carryAll(fresh, thread);
    } catch (e) {
      // `loaded` is left UNSET, which is what makes the failure recoverable:
      // the Retry button is this same call.
      setErrors((cur) => ({ ...cur, [task.key]: (e as Error).message }));
    } finally {
      setLoading((cur) => ({ ...cur, [task.key]: false }));
    }
  };

  // A row restored from memory was never TOGGLED, so nothing fetched the rest
  // of its thread. Same trip, made once, the first time a task list arrives.
  const restoredThreads = useRef(false);
  useEffect(() => {
    if (restoredThreads.current || tasks.length === 0) return;
    restoredThreads.current = true;
    for (const key of memory.current.expanded) {
      const task = tasks.find((t) => t.key === key);
      if (task && threadView(task).more) void showMore(task);
    }
  }, [tasks]);

  // ---- where the list stood ---------------------------------------------------
  // The list div is its own scroller, so this is one element's scrollTop.
  const listRef = useRef<HTMLDivElement | null>(null);
  // The offset still owed to the reader, or null once paid (or given up on).
  const owed = useRef<number | null>(memory.current.scroll || null);
  // The last offset THIS code set, so a scroll event can be told apart from
  // the reader's own.
  const settled = useRef<number | null>(null);

  const hasRows = tasks.length > 0;
  const hadRows = useRef(false);
  if (hasRows) hadRows.current = true;

  // Rows come back after a failed poll: the offset has to be waiting when they
  // do, so a stale empty ARMS the restore again.
  const staleEmptied = useRef(false);
  if (!hasRows && stale && hadRows.current) staleEmptied.current = true;
  useLayoutEffect(() => {
    if (!hasRows || !staleEmptied.current) return;
    staleEmptied.current = false;
    owed.current = memory.current.scroll || null;
    settled.current = null;
  }, [hasRows]);

  useLayoutEffect(() => {
    const el = listRef.current;
    if (owed.current === null || !el) return;
    const top = Math.min(owed.current, Math.max(el.scrollHeight - el.clientHeight, 0));
    if (Math.abs(el.scrollTop - top) > 1) el.scrollTop = top;
    settled.current = el.scrollTop;
    if (top >= owed.current - 1) owed.current = null;
  });

  // The restore window closes on its own, and opens on the FIRST ROWS, not on
  // mount; re-arms on every false→true.
  useEffect(() => {
    if (!hasRows) return;
    const t = setTimeout(() => {
      owed.current = null;
    }, RESTORE_WINDOW_MS);
    return () => clearTimeout(t);
  }, [hasRows]);

  // One writer for both halves, debounced; flushed on unmount.
  const writeTimer = useRef<number | null>(null);
  const remember = (next: ListMemory) => {
    memory.current = next;
    if (writeTimer.current !== null) clearTimeout(writeTimer.current);
    writeTimer.current = window.setTimeout(() => {
      writeTimer.current = null;
      writeListMemory(memory.current);
    }, WRITE_DEBOUNCE_MS);
  };
  useEffect(
    () => () => {
      if (writeTimer.current !== null) {
        clearTimeout(writeTimer.current);
        writeListMemory(memory.current);
      }
    },
    [],
  );

  useEffect(() => {
    remember({ ...memory.current, expanded: [...expanded] });
  }, [expanded]);

  // ---- the wheel works in the margins too -------------------------------------
  // The list is the scroller; a wheel anywhere on the page that no scroller of
  // its own claims is handed to it. The page root wears `data-tasks-page`
  // (Scheduled.tsx) — the one element of the pair that is always there.
  useEffect(() => {
    const page = document.querySelector<HTMLElement>("[data-tasks-page]");
    if (!page) return;
    const onWheel = (e: WheelEvent) => {
      const el = listRef.current;
      if (!el || !(e.target instanceof Element) || e.deltaY === 0) return;
      // Pinch-zoom arrives as ctrl+wheel; that is a zoom, not a scroll.
      if (e.ctrlKey) return;
      for (let n: Element | null = e.target; n && n !== page; n = n.parentElement) {
        const s = getComputedStyle(n);
        if (
          (s.overflowY === "auto" || s.overflowY === "scroll") &&
          n.scrollHeight > n.clientHeight
        ) {
          return;
        }
      }
      el.scrollTop += e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    };
    page.addEventListener("wheel", onWheel, { passive: true });
    return () => page.removeEventListener("wheel", onWheel);
  }, []);

  const onScroll = () => {
    const el = listRef.current;
    if (!el) return;
    const mine = settled.current !== null && Math.abs(el.scrollTop - settled.current) <= 1;
    settled.current = el.scrollTop;
    // A RESTORE IN PROGRESS WRITES NOTHING; only the reader's own scroll is
    // a statement about where they want to be.
    if (mine) return;
    owed.current = null;
    remember({ ...memory.current, scroll: el.scrollTop });
  };

  // An empty list is a position too (the top) — but only for a list that
  // EMPTIED, and only for an emptiness the server meant (not `stale`).
  useEffect(() => {
    if (hasRows || stale || !hadRows.current) return;
    owed.current = null;
    settled.current = null;
    remember({ ...memory.current, scroll: 0 });
  }, [hasRows, stale]);

  if (!hasRows) {
    return (
      <Empty className="border border-dashed border-border rounded-lg py-10">
        <EmptyHeader>
          <EmptyDescription>{emptyLabel}</EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  return (
    <>
      {/* WHERE AN UNARCHIVE WENT, at page level — the row's own note dies with
          the row when a status filter unmounts it. */}
      {pageNote && <Note>{pageNote}</Note>}
      {/* The frame is INSIDE the scroller so the bar stands beside it, not
          inside its border. */}
      <div className="tasks-list flex-1 min-h-0 overflow-y-auto scrollbar-auto-hide overscroll-contain" ref={listRef} onScroll={onScroll}>
        <EntityList>
          {/* ORDERED BY STATUS, NOT GROUPED BY IT (tasks-lib.sortByLane): no
              headers between the ranks; the order already says what they said. */}
          {rows.map((task) => (
            <TaskNode
              key={task.key}
              task={task}
              home={home}
              showProject={showProject}
              open={expanded.has(task.key)}
              selected={selected === task.key}
              onSelect={() => select(task.key)}
              onToggle={() => toggle(task)}
              onRetry={() => void showMore(task)}
              loaded={loaded[task.key]}
              loading={!!loading[task.key]}
              error={errors[task.key]}
              onEditEntry={onEditEntry}
              onReload={onReload}
              onPickProject={onPickProject}
              pinned={pinnedProjects.includes(task.project)}
              onPageNote={setPageNote}
              read={read}
              onRead={clear}
              onReadAll={clearAll}
              onUnreadAll={restoreAll}
              onSettleAll={settleAll}
            />
          ))}
        </EntityList>
      </div>
    </>
  );
}

function TaskNode({
  task,
  home,
  showProject,
  open: requested,
  selected,
  onSelect,
  onToggle,
  loaded,
  loading,
  onRetry,
  error,
  onEditEntry,
  onReload,
  onPickProject,
  pinned,
  onPageNote,
  read,
  onRead,
  onReadAll,
  onUnreadAll,
  onSettleAll,
}: {
  task: Task;
  home: string;
  showProject: boolean;
  open: boolean;
  selected: boolean;
  onSelect: () => void;
  onToggle: () => void;
  loaded?: TaskMessage[];
  loading: boolean;
  onRetry: () => void;
  error?: string;
  onEditEntry?: (entryId: string) => void;
  onReload?: () => void;
  onPickProject?: (project: string) => void;
  pinned?: boolean;
  onPageNote: (s: string) => void;
  read: Set<string>;
  onRead: (taskKey: string, m: TaskMessage) => void;
  onReadAll: (task: Task, held?: TaskMessage[]) => void;
  onUnreadAll: (taskKey: string, held: TaskMessage[]) => void;
  onSettleAll: (
    taskKey: string,
    held: TaskMessage[],
    answer: { unread: number },
  ) => void;
}) {
  // Is this row an accordion at all? `open` is DERIVED from the predicate so a
  // row that stops being expandable closes itself.
  const expandable = isExpandable(task);
  const open = expandable && requested;
  const view = threadView(task, loaded);
  // Everything this thread holds, one list (tasks-lib.heldMessages).
  const held = heldMessages(task, loaded);
  const unread = taskUnread(task, read, held);
  // What the thread holds AFTER an await — read from inside an in-flight write.
  const heldNow = useRef(held);
  useEffect(() => {
    heldNow.current = held;
  });
  // Open chat: where it goes and whether going there clears the thread — the
  // same function the Board card asks (openThreadIntent). Null = no session.
  const chat = openThreadIntent(task, unread);
  const label = firstLine(task.title) || "(untitled)";
  const ahead = isUpcomingTask(task);
  const taskFile_ = taskFile(task);
  // The scheduled run a ONE-MESSAGE UPCOMING row's press edits when it has no
  // conversation to open instead.
  const edit = onEditEntry ? upcomingEditEntry(task, held) : null;
  const when = taskWhen(task);
  const soon = nextRunChip(task);
  const outcome = outcomeTag(task);
  // Nothing but Unarchive on an archived row (showsRowActions).
  const acts = showsRowActions(task);
  const run = acts ? taskRunIntent(task) : null;
  const file = filingIntent(task);
  const seen = acts ? markReadIntent(task, read, held) : null;

  const [cancelling, setCancelling] = useState("");
  const [cancelErrors, setCancelErrors] = useState<Record<string, string>>({});
  // The task-level actions' own pair — run-now/re-send and archive share it.
  const [acting, setActing] = useState(false);
  const [note, setNote] = useState("");
  // THE FILING PRESS'S RECEIPT: up on the press, down when the pointer LEAVES
  // the row; while up, the mark slot shows the ring (now the new state) rather
  // than the hover-revealed opposite verb.
  const [refiled, setRefiled] = useState(false);

  const runNow = async (intent: TaskRunIntent) => {
    setActing(true);
    setNote("");
    try {
      const said = await performRun(intent);
      if (said) setNote(said);
    } catch (e) {
      // The server's own sentence, verbatim — a 409 reads as "wait".
      setNote((e as Error).message);
    } finally {
      setActing(false);
      onReload?.();
    }
  };

  const refile = async (intent: FilingIntent) => {
    setActing(true);
    setNote("");
    setRefiled(true);
    try {
      if (intent.kind === "archive") {
        await archiveTask(task.key);
      } else {
        // WHERE IT WENT goes to the page: a note on the row dies with the row.
        onPageNote(await performUnarchive(task.key));
      }
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setActing(false);
      onReload?.();
    }
  };

  // Mark the whole task read: local set FIRST, one server request, then
  // reconciled — a refusal takes the mark back, a non-zero remaining count wins.
  const markSeen = async () => {
    const wrote = held;
    const rollback = () => [...wrote, ...heldNow.current];
    setActing(true);
    setNote("");
    onReadAll(task, held);
    try {
      const answer = await markWholeTaskRead(task.key);
      if (answer.unread > 0) {
        onSettleAll(task.key, rollback(), answer);
        setNote(
          answer.unread === 1
            ? "1 message arrived while this was marking, and is still unread."
            : `${answer.unread} messages arrived while this was marking, and are still unread.`,
        );
      }
    } catch (e) {
      onUnreadAll(task.key, rollback());
      setNote((e as Error).message);
    } finally {
      setActing(false);
    }
  };

  const openMessage = (m: TaskMessage) => {
    onRead(task.key, m);
    const to = messageHref(task, m);
    if (!to) return;
    onSelect();
    navigateUrl(to);
  };

  /** A MESSAGE ROW's gesture: a message that HAS NOT GONE OUT opens the edit
   *  form on its own entry; anything that has run opens its turn. */
  const pressMessage = (m: TaskMessage) => {
    const entry = onEditEntry ? messageEditEntry(m) : null;
    if (entry) onEditEntry?.(entry);
    else openMessage(m);
  };

  // Open chat — the same performer the Board card spends (performOpen).
  const openChat = (intent: OpenThreadIntent) => {
    onSelect();
    performOpen(
      task,
      intent,
      {
        clearAll: onReadAll,
        restoreAll: onUnreadAll,
        settleAll: onSettleAll,
      },
      held,
    );
  };

  /** The task ROW's gesture, one function for mouse and keyboard: a row with a
   *  SESSION opens its thread; a row with NO session opens THE EDIT FORM on its
   *  scheduled run when it has one. */
  const activate = () => {
    if (chat) openChat(chat);
    else if (edit) onEditEntry?.(edit);
  };

  // WHERE the row's press goes as a URL, or null when it opens a modal. A real
  // stretched `<a href>` is what makes ⌘-click / middle click / "Open in new
  // tab" the browser's own behaviour.
  const href = chat?.href ?? null;
  // Does this row's press DO anything — may it claim to be a control at all?
  const pressable = href !== null || edit !== null;

  const cancel = async (m: TaskMessage, entryId: string) => {
    setCancelling(m.message_id);
    setCancelErrors((cur) => {
      const next = { ...cur };
      delete next[m.message_id];
      return next;
    });
    try {
      await cancelScheduledMessage(entryId);
    } catch (e) {
      // A 404 here is a real race: the loop may have sent it already.
      setCancelErrors((cur) => ({ ...cur, [m.message_id]: (e as Error).message }));
    } finally {
      setCancelling("");
      onReload?.();
    }
  };

  // A PRESS ON A TRAILING MARK IS A PRESS ON THE ROW (Akshil, 2026-08-27). The
  // marks sit above the stretched link for their hints, so they spend the
  // same gesture the link spends: modified press → new tab, plain → activate,
  // middle press → new tab and no autoscroll.
  const forward = {
    onClick: (e: React.MouseEvent) => {
      if (!href) return;
      if (opensElsewhere(e)) {
        window.open(href, "_blank", "noopener");
        return;
      }
      activate();
    },
    onAuxClick: (e: React.MouseEvent) => {
      if (e.button !== 1 || !href) return;
      e.preventDefault();
      window.open(href, "_blank", "noopener");
    },
    onMouseDown: (e: React.MouseEvent) => {
      if (e.button === 1 && href) e.preventDefault();
    },
  };

  return (
    <div className="tasks-node">
      <RowFrame
        className={cn(
          "tasks-row group/row py-1.5 pl-2 gap-2.5",
          selected && "bg-accent/30",
          !pressable && "cursor-default",
        )}
        interactive={pressable}
        data-refiled={refiled || undefined}
        // The row is a CONTAINER: with an href the stretched `<a>` is the
        // button, the tab stop and the name. The EDIT arm has no href, so that
        // row keeps role/tabIndex/keydown here.
        role={pressable && !href ? "button" : undefined}
        tabIndex={pressable && !href ? 0 : undefined}
        onClick={href ? undefined : pressable ? activate : undefined}
        onKeyDown={
          href
            ? undefined
            : (e) => {
                if (!pressable) return;
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  activate();
                }
              }
        }
        onMouseLeave={() => setRefiled(false)}
      >
        {/* The disclosure gutter is drawn WHETHER OR NOT there is a chevron in
            it, so the rail of rings never zigzags. It is the only way to
            expand a row (the row's own press opens the conversation), and its
            hit zone is grown past its ink with padding taken back by margins.
            It sits ABOVE the stretched link: gutter expands, everything else
            opens. */}
        {expandable ? (
          <button
            type="button"
            className="tasks-caret relative z-10 -my-2 -ml-2 flex h-9 w-7 shrink-0 items-center justify-center pl-2 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:text-foreground"
            aria-expanded={open}
            aria-label={open ? "Collapse messages" : "Expand messages"}
            title={open ? "Collapse messages" : "Expand messages"}
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
          >
            <ChevronRight
              className={cn("size-3.5 motion-safe:transition-transform", open && "rotate-90")}
              aria-hidden
            />
          </button>
        ) : (
          <span className="tasks-caret w-5 shrink-0" aria-hidden />
        )}
        {/* The row's navigation, as a real link stretched over the whole row.
            A MODIFIED press is left entirely alone — no SPA navigation and NO
            READ MARK. */}
        {href && (
          <RowLink
            href={href}
            aria-label={label}
            onClick={(e) => {
              if (opensElsewhere(e)) return;
              e.preventDefault();
              activate();
            }}
          />
        )}
        {/* THE MARK SLOT: the ring at rest (also the row's unread mark —
            filled while anything in the thread is unread), the Archive /
            Unarchive button under the pointer, in the same box. ONE button in
            this slot, never two (filingIntent answers with a direction). Not
            rendered at all on a task with nothing to file. */}
        <span className="tasks-rowmark relative z-10 flex size-3.5 shrink-0 items-center justify-center">
          <span
            className={cn(
              "flex items-center",
              file && "group-hover/row:opacity-0 group-has-[.tasks-act:focus-visible]/row:opacity-0 group-data-[refiled]/row:opacity-100 motion-safe:transition-opacity",
            )}
          >
            <StatusIcon
              status={taskColumn(task)}
              failed={task.failed}
              unread={unread > 0}
              count={unread}
              pulse={task.live}
            />
          </span>
          {file && (
            <RowAction
              className="absolute -inset-1.5 h-auto w-auto"
              title={file.title}
              aria-label={file.label}
              disabled={acting}
              onClick={(e) => {
                e.stopPropagation();
                void refile(file);
              }}
            >
              {file.kind === "archive" ? <Archive className={ICON} /> : <ArchiveRestore className={ICON} />}
            </RowAction>
          )}
        </span>
        <IdChip id={task.task_id} />
        {/* Beside the id, the same component in the same place as on the card. */}
        {outcome && <OutcomePill text={outcome.text} title={outcome.title} />}
        {/* Greyed while the work is still ahead of it. THE TITLE is the element
            that ellipsises, so it is the one that carries the caption. */}
        <span
          className={cn("truncate font-medium relative z-10 pointer-events-none", ahead && "text-muted-foreground font-normal")}
          data-hint={task.title}
        >
          {label}
        </span>
        {/* The one thing that follows the title: a file mark, on tasks about a
            single document rather than the folder. */}
        {taskFile_ ? (
          <span
            className="relative z-10 flex shrink-0 items-center text-muted-foreground"
            data-hint={tildePath(taskFile_, home)}
            aria-label={`This task is about ${basename(taskFile_)}`}
            {...forward}
          >
            <FileText className="size-3" aria-hidden />
          </span>
        ) : null}

        {/* Exactly ONE auto margin in this row. */}
        <span className="tasks-grow flex-1" />

        {/* The strip is behind SHOW_ROW_ACTIONS: "clear it, run it, open it". */}
        {SHOW_ROW_ACTIONS && seen && (
          <RowAction
            title={seen.title}
            aria-label={seen.label}
            disabled={acting}
            onClick={(e) => {
              e.stopPropagation();
              void markSeen();
            }}
          >
            <CheckCheck className={ICON} />
          </RowAction>
        )}
        {SHOW_ROW_ACTIONS && run && (
          <RowAction
            title={run.title}
            aria-label={run.label}
            disabled={acting}
            onClick={(e) => {
              e.stopPropagation();
              void runNow(run);
            }}
          >
            {run.rerun ? <RotateCcw className={ICON} /> : <Play className={ICON} />}
          </RowAction>
        )}
        {SHOW_ROW_ACTIONS && chat && (
          <RowAction
            title="Open chat"
            aria-label="Open chat"
            onClick={(e) => {
              e.stopPropagation();
              openChat(chat);
            }}
          >
            <ExternalLink className={ICON} />
          </RowAction>
        )}
        {/* Folder FIRST, count, run ahead, time LAST — the time is what
            changes, so it sits where the eye lands. */}
        {showProject && (
          <FolderChip
            name={basename(task.project)}
            title={tildePath(task.project, home)}
            onPick={onPickProject && (() => onPickProject(task.project))}
            active={pinned}
          />
        )}
        {/* HOW MANY MESSAGES this task holds. Always drawn, never below one:
            a hole in a column reads as a broken row. The noun survives as the
            aria-label. */}
        {(() => {
          const shown = Math.max(1, task.message_count);
          return (
            <Tiny
              className="relative z-10 inline-flex items-center gap-1 tabular-nums shrink-0"
              aria-label={`${shown} message${shown === 1 ? "" : "s"}`}
              data-hint={`${shown} message${shown === 1 ? "" : "s"} in this task`}
              {...forward}
            >
              {shown}
              <MessageSquare className="size-3" aria-hidden />
            </Tiny>
          );
        })()}
        {/* The run still to come, when the time beside it is not already it. */}
        {soon && (
          <Tiny className="relative z-10 shrink-0 whitespace-nowrap" data-hint={soon.title} {...forward}>
            {soon.text}
          </Tiny>
        )}
        {/* ALWAYS drawn: taskWhen hands back an em dash rather than nothing. */}
        <Tiny className="relative z-10 w-14 shrink-0 text-right tabular-nums whitespace-nowrap" data-hint={when.title} {...forward}>
          {when.text}
        </Tiny>
      </RowFrame>

      {note && <Note className="px-4 py-1 border-b border-border">{note}</Note>}

      {open && (
        <div className="tasks-thread bg-muted/20 border-b border-border last:border-b-0">
          {view.messages.map((m) => {
            const tone = threadTone(task, m);
            const mark = unreadMarker(task.key, m, read);
            const isNew = mark.unread;
            const stop = cancelIntent(m);
            // The one entry this message can be edited as, or null when its
            // press means the transcript instead.
            const fix = onEditEntry ? messageEditEntry(m) : null;
            // Where this row's press GOES, or null (edit form / nowhere).
            const to = fix ? null : openMessageHref(task, m);
            const busy = cancelling === m.message_id;
            const why = cancelErrors[m.message_id];
            return (
              <Fragment key={m.message_id}>
                <RowFrame
                  className={cn("tasks-msg group/row py-1 pl-[3.25rem] gap-2.5 text-xs border-b-0", isNew && "font-semibold")}
                  interactive
                  role={to ? undefined : "button"}
                  tabIndex={to ? undefined : 0}
                  onClick={to ? undefined : () => pressMessage(m)}
                  onKeyDown={
                    to
                      ? undefined
                      : (e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            pressMessage(m);
                          }
                        }
                  }
                >
                  {to && (
                    <RowLink
                      href={to}
                      aria-label={firstLine(m.body) || "(empty)"}
                      onClick={(e) => {
                        if (opensElsewhere(e)) return;
                        e.preventDefault();
                        pressMessage(m);
                      }}
                    />
                  )}
                  {/* The leaf's ring and its unread mark. No `count`: one
                      unread message's dot means "unread" outright. */}
                  <StatusIcon
                    status={tone.column}
                    failed={tone.failed}
                    label={tone.label}
                    unread={isNew}
                    className="size-3"
                  />
                  <IdChip id={m.message_id} />
                  <span className="truncate relative z-10 pointer-events-none" data-hint={m.body}>
                    {firstLine(m.body) || "(empty)"}
                  </span>
                  <span className="tasks-grow flex-1" />
                  {SHOW_ROW_ACTIONS && (
                    <>
                      {fix && (
                        <RowAction
                          title="Edit"
                          aria-label="Edit"
                          onClick={(e) => {
                            e.stopPropagation();
                            onEditEntry?.(fix);
                          }}
                        >
                          <Pencil className={ICON} />
                        </RowAction>
                      )}
                      {stop && (
                        <RowAction
                          title={stop.title}
                          aria-label={stop.label}
                          disabled={busy}
                          onClick={(e) => {
                            e.stopPropagation();
                            void cancel(m, stop.id);
                          }}
                        >
                          {stop.scope === "occurrence" ? <SkipForward className={ICON} /> : <Ban className={ICON} />}
                        </RowAction>
                      )}
                    </>
                  )}
                  {/* ONE time per row, relative, the absolute instant (and a
                      late/early run) in the tooltip. */}
                  <Tiny className="w-14 shrink-0 text-right tabular-nums whitespace-nowrap font-normal" data-hint={messageWhenTitle(m)}>
                    {relativeWhen(m.at)}
                  </Tiny>
                </RowFrame>
                {why && <p className="px-[3.25rem] py-1 text-xs text-destructive">{why}</p>}
              </Fragment>
            );
          })}

          {/* A FAILED FETCH HAS TO BE RECOVERABLE WHERE IT HAPPENED: the same
              call the disclosure makes, beside the failure it is about. */}
          {error && (
            <p className="flex items-center gap-2 px-[3.25rem] py-1.5 text-xs text-destructive" role="alert">
              <span className="truncate">{error}</span>
              <Button variant="outline" size="xs" disabled={loading} onClick={onRetry}>
                Retry
              </Button>
            </p>
          )}

          {/* Not a control: expanding fetches the whole thread by itself; this
              names the NUMBER still coming so a long thread does not look like
              a short one that finished. */}
          {loading && (
            <Tiny className="block px-[3.25rem] py-1.5 shimmer-text" aria-live="polite">
              {view.hidden > 0 ? `Loading ${view.hidden} more…` : "Loading…"}
            </Tiny>
          )}
        </div>
      )}
    </div>
  );
}

// ---- Board view: columns of tasks --------------------------------------------

// A lane opens on twenty cards and reveals twenty at a time.
const LANE_INITIAL_VISIBLE = 20;
const LANE_REVEAL = 20;

// Which lanes are rolled up into the rail. The RULE lives in tasks-lib; this is
// only the remembered half (a lane with cards). A blocked store costs the
// memory, never the board.
function readLaneChoices(): LaneChoices {
  try {
    return parseLaneChoices(localStorage.getItem(LANE_CHOICE_KEY));
  } catch {
    return {};
  }
}

export function TaskBoard({
  tasks,
  home = "",
  onReload,
}: {
  /** Already filtered, in the SERVER's order — the LANES re-order it. */
  tasks: Task[];
  home?: string;
  /** Re-read the list after a drop lands (or fails). */
  onReload: () => void;
}) {
  const [choices, setChoices] = useState<LaneChoices>(readLaneChoices);
  // Lanes the reader has opened WHILE EMPTY — a peek, component state only.
  const [peeked, setPeeked] = useState<Set<BoardColumn>>(() => new Set());
  const [visible, setVisible] = useState<Record<string, number>>({});
  // The card in flight and the lane under it. Native HTML5 drag.
  const [dragging, setDragging] = useState<Task | null>(null);
  const [overLane, setOverLane] = useState<BoardColumn | null>(null);
  // What the server said about the last move the board asked for.
  const [note, setNote] = useState<string | null>(null);
  // The whole-task half of the read bookkeeping the List uses.
  const { read, clearAll, restoreAll, settleAll } = useReadSet();

  const showProject = useMemo(() => spansProjects(tasks), [tasks]);

  const allowed = useMemo(
    () => new Set(dragging ? dropLanes(dragging) : []),
    [dragging],
  );

  // The one lane whose drop is not a filing decision, named while the card is
  // still in the air because "run this now" is not undoable.
  const runLane = useMemo(() => {
    if (!dragging) return null;
    for (const col of BOARD_COLUMNS) {
      if (dropAction(dragging, col.key)?.kind === "run") return col.key;
    }
    return null;
  }, [dragging]);

  const drop = async (lane: BoardColumn) => {
    const task = dragging;
    setDragging(null);
    setOverLane(null);
    if (!task || !allowed.has(lane)) return;
    // File, un-file, or run early — tasks-lib's decision (dropAction).
    const action = dropAction(task, lane);
    if (!action) return;
    setNote(null);
    try {
      if (action.kind === "run") {
        await runScheduledNow(action.entryId);
      } else if (action.kind === "unarchive") {
        setNote(await performUnarchive(task.key));
      } else {
        await archiveTask(task.key);
      }
    } catch (e) {
      setNote((e as Error).message);
    }
    onReload();
  };

  // The same two calls the drop makes, asked for by a card's own button.
  const refile = async (task: Task, intent: FilingIntent) => {
    setNote(null);
    try {
      if (intent.kind === "archive") {
        await archiveTask(task.key);
      } else {
        setNote(await performUnarchive(task.key));
      }
    } catch (e) {
      setNote((e as Error).message);
    }
    onReload();
  };

  // Run now / Re-run from a card; the refusal lands in the board's one note.
  const runNow = async (intent: TaskRunIntent) => {
    setNote(null);
    try {
      const said = await performRun(intent);
      if (said) setNote(said);
    } catch (e) {
      setNote((e as Error).message);
    }
    onReload();
  };

  // Opening a card: the conversation, and the unread cleared on the way out.
  const openCard = (task: Task, intent: OpenThreadIntent) => {
    performOpen(task, intent, { clearAll, restoreAll, settleAll }, heldMessages(task));
  };

  // Shared by expanded lane bodies AND collapsed rails.
  const dropProps = (lane: BoardColumn) => ({
    onDragOver: (ev: ReactDragEvent) => {
      if (!dragging || !allowed.has(lane)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      if (overLane !== lane) setOverLane(lane);
    },
    onDragLeave: () => {
      if (overLane === lane) setOverLane(null);
    },
    onDrop: (ev: ReactDragEvent) => {
      ev.preventDefault();
      void drop(lane);
    },
  });

  // A press on a lane with cards is a preference and is written down; a press
  // on an empty one is a peek and stays in memory.
  const toggleLane = (key: BoardColumn, nowCollapsed: boolean) => {
    if ((byLane.get(key)?.length ?? 0) === 0) {
      setPeeked((cur) => {
        const next = new Set(cur);
        if (nowCollapsed) next.add(key);
        else next.delete(key);
        return next;
      });
      return;
    }
    setChoices((cur) => {
      const next = { ...cur, [key]: !nowCollapsed };
      try {
        localStorage.setItem(LANE_CHOICE_KEY, JSON.stringify(next));
      } catch {
        // best-effort; a full or blocked store never breaks the board
      }
      return next;
    });
  };

  const byLane = useMemo(() => groupByColumn(tasks), [tasks]);

  // A peek dies the moment the lane it was about stops being empty.
  useEffect(() => {
    setPeeked((cur) => {
      if (cur.size === 0) return cur;
      const next = new Set([...cur].filter((key) => (byLane.get(key)?.length ?? 0) === 0));
      return next.size === cur.size ? cur : next;
    });
  }, [byLane]);

  const dropClasses = (key: BoardColumn) =>
    cn(
      dragging && allowed.has(key) && "outline-dashed outline-1 outline-ring -outline-offset-1",
      runLane === key && "outline-ring outline-2",
      overLane === key && "bg-accent/40",
    );

  return (
    <>
      {note && <Note>{note}</Note>}
      <div className="schedule-tv-board flex flex-1 min-h-0 gap-3 overflow-x-auto scrollbar-auto-hide items-stretch">
        {BOARD_COLUMNS.map((col) => {
          const lane = byLane.get(col.key) ?? [];
          // How many CARDS still hold something unread — the header's mark.
          const news = laneUnread(lane, read);
          const rolled = laneRolledUp(col.key, lane.length, choices, peeked);
          if (rolled) {
            // An empty rail is STILL A BUTTON and still a drop target.
            const empty = lane.length === 0;
            return (
              <button
                type="button"
                key={col.key}
                className={cn(
                  "schedule-tv-rail flex w-[52px] shrink-0 flex-col items-center gap-3 rounded-lg border border-border bg-muted/30 py-3 hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
                  dropClasses(col.key),
                )}
                title={
                  runLane === col.key
                    ? "Run the next scheduled message now"
                    : empty
                      ? `${col.label}: nothing yet`
                      : `${col.label}: ${lane.length}`
                }
                onClick={() => toggleLane(col.key, true)}
                {...dropProps(col.key)}
              >
                <StatusIcon status={col.key} unread={news > 0} count={news} />
                <SectionHeading className="[writing-mode:vertical-rl] rotate-180 text-xs whitespace-nowrap">
                  {col.label}
                </SectionHeading>
                {!empty && <Tiny className="tabular-nums">{lane.length}</Tiny>}
              </button>
            );
          }
          const shown = visible[col.key] ?? LANE_INITIAL_VISIBLE;
          const cards = lane.slice(0, shown);
          const hidden = Math.max(lane.length - cards.length, 0);
          return (
            <div className="schedule-tv-lane flex w-[260px] shrink-0 flex-col min-h-0 rounded-lg border border-border bg-muted/30" key={col.key}>
              <button
                type="button"
                className="flex items-center gap-2 px-3 py-2 text-left hover:bg-muted/50 focus-visible:outline-none focus-visible:bg-muted/50 rounded-t-lg"
                title={`Collapse ${col.label}`}
                onClick={() => toggleLane(col.key, false)}
              >
                <StatusIcon status={col.key} unread={news > 0} count={news} />
                <SectionHeading className="text-xs flex-1 truncate">{col.label}</SectionHeading>
                <Tiny className="tabular-nums">{lane.length}</Tiny>
              </button>
              <div
                className={cn(
                  "flex flex-1 min-h-0 flex-col gap-2 overflow-y-auto scrollbar-auto-hide p-2 pt-0 rounded-b-lg",
                  dropClasses(col.key),
                )}
                {...dropProps(col.key)}
              >
                {runLane === col.key && (
                  <Tiny className="px-1 py-1">Run now — the time stays put</Tiny>
                )}
                {cards.map((task) => (
                  <TaskCard
                    key={task.key}
                    task={task}
                    home={home}
                    showProject={showProject}
                    unread={taskUnread(task, read)}
                    isDragging={dragging?.key === task.key}
                    onDragStart={() => setDragging(task)}
                    onDragEnd={() => {
                      setDragging(null);
                      setOverLane(null);
                    }}
                    onFile={(intent) => refile(task, intent)}
                    onRun={runNow}
                    onOpen={(intent) => openCard(task, intent)}
                  />
                ))}
                {hidden > 0 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-muted-foreground"
                    onClick={() =>
                      setVisible((cur) => ({
                        ...cur,
                        [col.key]: (cur[col.key] ?? LANE_INITIAL_VISIBLE) + LANE_REVEAL,
                      }))
                    }
                  >
                    Show {Math.min(LANE_REVEAL, hidden)} more
                  </Button>
                )}
                {lane.length > 0 && (hidden > 0 || lane.length >= shown) && (
                  <Tiny className="px-1 pb-1 text-center">
                    Showing {cards.length} of {lane.length}
                  </Tiny>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

function TaskCard({
  task,
  home,
  showProject,
  unread,
  isDragging,
  onDragStart,
  onDragEnd,
  onFile,
  onRun,
  onOpen,
}: {
  task: Task;
  home: string;
  showProject: boolean;
  unread: number;
  isDragging: boolean;
  onDragStart: () => void;
  onDragEnd: () => void;
  onFile: (intent: FilingIntent) => Promise<void>;
  onRun: (intent: TaskRunIntent) => Promise<void>;
  onOpen: (intent: OpenThreadIntent) => void;
}) {
  // A card that can neither triage nor run must not lift (tasks-lib.dropLanes).
  const draggable = isDraggable(task);
  // Where the click goes and whether it clears the thread — tasks-lib's answer.
  const open = openThreadIntent(task, unread);
  const file = filingIntent(task);
  const run = showsRowActions(task) ? taskRunIntent(task) : null;
  const soon = nextRunChip(task);
  const outcome = outcomeTag(task);
  const lane = taskColumn(task);
  // The ring earns its place on a card by DISAGREEING with the lane: a failed
  // task filed under a header that does not say so.
  const failedOffLane = isFailedTask(task) && lane !== "failed";
  const [busy, setBusy] = useState(false);
  const refile = async (intent: FilingIntent) => {
    setBusy(true);
    try {
      await onFile(intent);
    } finally {
      setBusy(false);
    }
  };
  const runNow = async (intent: TaskRunIntent) => {
    setBusy(true);
    try {
      await onRun(intent);
    } finally {
      setBusy(false);
    }
  };
  return (
    // A wrapper, because the card IS a button and a button cannot hold one:
    // the actions are SIBLINGS pinned over the card's head, so pressing them
    // cannot bubble into the card's own click.
    <div className={cn("tasks-card-wrap group/row relative", isDragging && "opacity-50")}>
      <CardFrame
        className={cn("schedule-tv-card", draggable && "cursor-grab active:cursor-grabbing", isDragging && "ring-ring")}
        data-hint={task.title}
        draggable={draggable}
        onDragStart={(ev) => {
          // Some data is required for Firefox to start a drag at all.
          ev.dataTransfer.setData("text/plain", task.key);
          ev.dataTransfer.effectAllowed = "move";
          onDragStart();
        }}
        onDragEnd={onDragEnd}
        onClick={() => {
          if (open) onOpen(open);
        }}
      >
        {/* The head is the id — and a ring only on the card whose status its
            lane does not mention. Held at one height either way. */}
        <span className="flex h-4 items-center gap-2 pr-6">
          {failedOffLane && <StatusIcon status={lane} failed />}
          {task.live && <StatusIcon status="in_progress" label="Running" pulse className="size-3" />}
          <IdChip id={task.task_id} />
          {outcome && <OutcomePill text={outcome.text} title={outcome.title} />}
        </span>
        {/* UNREAD IS THE TITLE'S WEIGHT; the words are added for the tree. */}
        <span className={cn("text-sm leading-snug [overflow-wrap:anywhere]", unread > 0 && "font-semibold")}>
          {firstLine(task.title) || "(untitled)"}
          {unread > 0 && <span className="sr-only">{`, ${taskUnreadLabel(unread)}`}</span>}
        </span>
        {(showProject || soon) && (
          <span className="flex items-center gap-2 min-w-0">
            {showProject && (
              <FolderChip name={basename(task.project)} title={tildePath(task.project, home)} />
            )}
            {soon && (
              <Tiny className="truncate" title={soon.title}>
                {soon.text}
              </Tiny>
            )}
          </span>
        )}
      </CardFrame>
      {(file || (SHOW_ROW_ACTIONS && run)) && (
        <span className="absolute right-1.5 top-1.5 flex items-center gap-0.5">
          {SHOW_ROW_ACTIONS && run && (
            <RowAction
              title={run.title}
              aria-label={`${run.label} ${task.task_id}`}
              disabled={busy}
              onClick={() => void runNow(run)}
            >
              {run.rerun ? <RotateCcw className={ICON} /> : <Play className={ICON} />}
            </RowAction>
          )}
          {file && (
            <RowAction
              title={file.title}
              aria-label={`${file.label} ${task.task_id}`}
              disabled={busy}
              onClick={() => void refile(file)}
            >
              {file.kind === "archive" ? <Archive className={ICON} /> : <ArchiveRestore className={ICON} />}
            </RowAction>
          )}
        </span>
      )}
    </div>
  );
}
