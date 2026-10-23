// Bulk archive for the Tasks views: the selection, the confirm for running
// tasks, the optimistic hide, the one Undo toast, and the small controls the
// views draw (group checkbox, row checkbox, bar). The data rules are in
// task-selection.ts; the single-task archive is `archiveTask`, unchanged, so a
// bulk archive tears down exactly what a single one does.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { archiveTask, unarchiveTask } from "@platform/lib/api";
import type { TeardownReport } from "@platform/lib/api";
import type { Task } from "@platform/lib/api";
import { notify } from "@platform/lib/notifications";
import { ConfirmDialog } from "@apps/explorer/FsDialogs";
import { beginArchiveBatch, closedToastText } from "@shell/teardown-notice";
import { useTaskSelection, type TaskSelection } from "@shell/useTaskSelection";
import {
  archivedLabel,
  archivePlan,
  BULK_CONCURRENCY,
  BULK_UNDO_MS,
  confirmText,
  groupState,
  runPool,
} from "@shell/task-selection";

/** What the views hand to a header or a row to draw its checkbox. */
export interface BulkSelect {
  selection: TaskSelection;
  /** May this task be selected at all? (An archived task cannot be archived.) */
  selectable: (task: Task) => boolean;
}

export function useBulkArchive({
  tasks: tasksIn,
  selectable,
  resetKey = "",
  onReload,
}: {
  tasks: Task[];
  selectable: (task: Task) => boolean;
  resetKey?: string;
  onReload?: () => void;
}) {
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const [confirm, setConfirm] = useState<{ keys: string[]; running: number } | null>(null);
  const [undo, setUndo] = useState<{ keys: string[]; text: string } | null>(null);
  const timer = useRef<number | null>(null);
  const tasks = useMemo(() => (hidden.size === 0 ? tasksIn : tasksIn.filter((t) => !hidden.has(t.key))), [tasksIn, hidden]);
  const visibleKeys = useMemo(() => tasks.filter(selectable).map((t) => t.key), [tasks, selectable]);
  const selection = useTaskSelection(visibleKeys, resetKey);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );
  // A hidden key is forgotten once the data agrees (the task reads archived or
  // is gone), so a task that comes back is not held hidden for ever.
  useEffect(() => {
    setHidden((h) => {
      if (h.size === 0) return h;
      const live = new Set(tasksIn.filter((t) => t.status !== "archived").map((t) => t.key));
      const keep = [...h].filter((k) => live.has(k));
      return keep.length === h.size ? h : new Set(keep);
    });
  }, [tasksIn]);

  const latest = useRef({ onReload });
  useLayoutEffect(() => {
    latest.current = { onReload };
  });

  const run = useCallback(
    async (keys: string[]): Promise<void> => {
      if (keys.length === 0) return;
      setHidden((h) => new Set([...h, ...keys]));
      selection.clear();
      const batch = beginArchiveBatch(keys);
      const reports: Record<string, TeardownReport | undefined> = {};
      const results = await runPool(keys, BULK_CONCURRENCY, (k) =>
        archiveTask(k).then((r) => {
          reports[k] = r.teardown;
        }),
      );
      batch.end();
      const failed = results.filter((r) => !r.ok).map((r) => r.item);
      const done = results.filter((r) => r.ok).map((r) => r.item);
      if (failed.length > 0) {
        setHidden((h) => {
          const n = new Set(h);
          for (const k of failed) n.delete(k);
          return n;
        });
        notify({
          title: failed.length === 1 ? "Could not archive 1 task" : `Could not archive ${failed.length} tasks`,
          detail: results.find((r) => !r.ok)?.error?.message,
          tone: "error",
        });
      }
      if (done.length > 0) {
        setUndo({ keys: done, text: closedToastText(archivedLabel(done.length), done.map((k) => reports[k])) });
        if (timer.current !== null) window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setUndo(null), BULK_UNDO_MS);
      }
      latest.current.onReload?.();
    },
    [selection],
  );

  /** The bar's Archive, and the Workspace's `x` with a selection. */
  const archiveSelected = useCallback((): void => {
    const plan = archivePlan(tasks, selection.selected);
    if (plan.keys.length === 0) return;
    if (plan.running > 0) setConfirm(plan);
    else void run(plan.keys);
  }, [tasks, selection.selected, run]);

  const undoAll = useCallback((): void => {
    const keys = undo?.keys ?? [];
    setUndo(null);
    if (timer.current !== null) window.clearTimeout(timer.current);
    void runPool(keys, BULK_CONCURRENCY, (k) => unarchiveTask(k)).then((results) => {
      const back = new Set(results.filter((r) => r.ok).map((r) => r.item));
      setHidden((h) => new Set([...h].filter((k) => !back.has(k))));
      const bad = results.filter((r) => !r.ok).length;
      if (bad > 0) notify({ title: bad === 1 ? "Could not reopen 1 task" : `Could not reopen ${bad} tasks`, tone: "error" });
      latest.current.onReload?.();
    });
  }, [undo]);

  const node: ReactNode = (
    <>
      {confirm && (
        <ConfirmDialog
          title="Archive tasks"
          message={confirmText(confirm.keys.length, confirm.running)}
          confirmLabel="Archive"
          danger
          onConfirm={() => {
            const keys = confirm.keys;
            setConfirm(null);
            void run(keys);
          }}
          onCancel={() => setConfirm(null)}
        />
      )}
      {undo && (
        <div className="ws-undo" role="status" data-bulk-undo>
          <span className="ws-undo-text">{undo.text}</span>
          <button type="button" className="ws-undo-btn" onClick={undoAll}>
            Undo
          </button>
        </div>
      )}
    </>
  );

  const bulk = useMemo<BulkSelect>(() => ({ selection, selectable }), [selection, selectable]);
  return { tasks, hidden, selection, archiveSelected, node, bulk };
}

/** "N selected  [Archive] [Clear]" — styled with the toolbar's own pieces. */
export function BulkBar({
  count,
  onArchive,
  onClear,
}: {
  count: number;
  onArchive: () => void;
  onClear: () => void;
}) {
  if (count === 0) return null;
  return (
    <div className="tasks-bulk-bar" role="toolbar" aria-label="Selected tasks">
      <span className="tasks-bulk-count">{count} selected</span>
      <button type="button" className="tasks-bulk-btn tasks-bulk-archive" onClick={onArchive}>
        Archive
      </button>
      <button type="button" className="tasks-bulk-btn" onClick={onClear}>
        Clear
      </button>
    </div>
  );
}

/** A group heading's select-all. Outside the fold's button, or stopped before
 *  it, so it never folds the group. `keys` is EVERY selectable task in the
 *  group, drawn or not (a capped or folded group included). */
export function GroupCheck({ select, keys, name }: { select: BulkSelect; keys: readonly string[]; name: string }) {
  const ref = useRef<HTMLInputElement | null>(null);
  const state = groupState(select.selection.selected, keys);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = state === "some";
  }, [state]);
  if (keys.length === 0) return null;
  return (
    <input
      ref={ref}
      type="checkbox"
      className="tasks-check tasks-group-check"
      aria-label={`Select all in ${name}`}
      checked={state === "all"}
      onClick={(e) => e.stopPropagation()}
      onChange={() => select.selection.setMany(keys, state !== "all")}
    />
  );
}

/** A task's own checkbox. Pressing it toggles that task only: it never opens
 *  the task or its peek. */
export function RowCheck({ select, task, className = "" }: { select: BulkSelect; task: Task; className?: string }) {
  if (!select.selectable(task)) return null;
  return (
    <input
      type="checkbox"
      className={("tasks-check tasks-row-check " + className).trim()}
      aria-label={`Select ${task.title || "task"}`}
      checked={select.selection.has(task.key)}
      onClick={(e) => e.stopPropagation()}
      onChange={() => select.selection.toggle(task.key)}
    />
  );
}
