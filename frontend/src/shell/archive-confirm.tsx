// The one ask before a single-task archive stops a running task. The bulk bar
// has its own dialog (TaskBulk); every other door (Board drag and card button,
// List row, Workspace key and ring slot) goes through this hook, with the same
// wording (`confirmText`). A task that is not running archives with no prompt.
import { useCallback, useState } from "react";
import type { ReactNode } from "react";
import { ConfirmDialog } from "@apps/explorer/FsDialogs";
import { archiveNeedsConfirm, confirmText } from "@shell/task-selection";

export function useArchiveConfirm(): {
  /** Run `go` now, or after the reader agrees when `task` is running. */
  guard: (task: { status: string }, go: () => void) => void;
  /** Render once, anywhere in the component. */
  node: ReactNode;
} {
  const [pending, setPending] = useState<{ go: () => void } | null>(null);
  const guard = useCallback((task: { status: string }, go: () => void): void => {
    if (archiveNeedsConfirm(task)) setPending({ go });
    else go();
  }, []);
  const node: ReactNode = pending ? (
    <ConfirmDialog
      title="Archive task"
      message={confirmText(1, 1)}
      confirmLabel="Archive"
      danger
      onConfirm={() => {
        const go = pending.go;
        setPending(null);
        go();
      }}
      onCancel={() => setPending(null)}
    />
  ) : null;
  return { guard, node };
}
