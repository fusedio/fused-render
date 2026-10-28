// What archiving KEPT, said to the reader (Workspace scope, unit 3).
//
// The server removes a task's clean, pushed worktrees on archive and keeps the
// rest. A kept folder with no word about it is a leak the reader finds weeks
// later, so every archive, from any view, passes through `api.onTaskArchived`
// and a kept worktree becomes one notification naming the folder and the reason,
// with "Remove anyway" as its action.
//
// The wording is pure (`keptReasonText`, `keptWorktreeNotice`); the component at
// the foot only registers the listener and spends the answer on `notify`.
import { useEffect } from "react";
import { onTaskArchived, removeTaskWorktrees } from "@platform/lib/api";
import type { TeardownKept, TeardownReport } from "@platform/lib/api";
import { dismissNotification, notify } from "@platform/lib/notifications";

const plural = (n: number, one: string, many: string): string => `${n} ${n === 1 ? one : many}`;

/** Why one worktree was kept, in the reader's words. */
export function keptReasonText(k: TeardownKept): string {
  const out: string[] = [];
  for (const r of k.reasons) {
    if (r === "dirty") out.push(plural(k.dirty_files ?? 0, "uncommitted file", "uncommitted files"));
    else if (r === "unpushed") out.push(plural(k.unpushed ?? 0, "unpushed commit", "unpushed commits"));
    else if (r === "unknown") out.push("could not read its git state");
    else if (r === "not-managed") out.push("not made by Fused Render");
    else if (r === "remove-failed") out.push("git would not remove it");
    else if (r === "in use") out.push("another task or a live session is using it");
  }
  return out.join(", ");
}

const folderName = (path: string): string => path.split(/[\\/]/).filter(Boolean).pop() ?? path;

export interface KeptNotice {
  title: string;
  detail: string;
  paths: string[];
}

/** The notice for an archive's report, or null when nothing was kept. */
export function keptWorktreeNotice(
  _key: string,
  report: TeardownReport | undefined,
): KeptNotice | null {
  const kept = report?.kept ?? [];
  if (kept.length === 0) return null;
  const paths = kept.map((k) => k.path);
  if (kept.length === 1) {
    return {
      title: `Kept worktree ${folderName(kept[0].path)}`,
      detail: keptReasonText(kept[0]),
      paths,
    };
  }
  return {
    title: `Kept ${kept.length} worktrees`,
    detail: kept.map((k) => `${folderName(k.path)}: ${keptReasonText(k)}`).join("; "),
    paths,
  };
}

export interface BatchKeptNotice extends KeptNotice {
  /** Which task's worktrees to remove, for the one "Remove anyway". */
  byKey: { key: string; paths: string[] }[];
}

/** ONE notice for a bulk archive: every task's kept worktrees together, or
 *  null when nothing was kept. A batch of one is the single archive's notice. */
export function batchKeptNotice(entries: { key: string; report: TeardownReport | undefined }[]): BatchKeptNotice | null {
  const withKept = entries.filter((e) => (e.report?.kept ?? []).length > 0);
  if (withKept.length === 0) return null;
  const byKey = withKept.map((e) => ({ key: e.key, paths: e.report!.kept.map((k) => k.path) }));
  if (withKept.length === 1) {
    const one = keptWorktreeNotice(withKept[0].key, withKept[0].report);
    return one ? { ...one, byKey } : null;
  }
  const kept = withKept.flatMap((e) => e.report!.kept);
  return {
    title: `Kept ${kept.length} worktrees from ${withKept.length} archived tasks`,
    detail: kept.map((k) => `${folderName(k.path)}: ${keptReasonText(k)}`).join("; "),
    paths: kept.map((k) => k.path),
    byKey,
  };
}

/** What an archive actually did, for the Undo toast: "stopped 2 processes,
 *  removed 1 worktree", or "" when it did neither. Undo only un-files the task,
 *  so the toast says out loud what it cannot bring back. */
export function teardownSummary(reports: readonly (TeardownReport | undefined)[]): string {
  let procs = 0;
  let removed = 0;
  for (const r of reports) {
    if (!r) continue;
    procs += (r.stopped ?? 0) + (r.killed?.length ?? 0);
    removed += r.removed?.length ?? 0;
  }
  const parts: string[] = [];
  if (procs > 0) parts.push(`stopped ${plural(procs, "process", "processes")}`);
  if (removed > 0) parts.push(`removed ${plural(removed, "worktree", "worktrees")}`);
  return parts.join(", ");
}

/** The toast line: `label` alone when nothing was torn down, else the label, what
 *  went, and the Undo caveat. */
export function closedToastText(label: string, reports: readonly (TeardownReport | undefined)[]): string {
  const did = teardownSummary(reports);
  return did ? `${label} \u2014 ${did}; Undo won't restart them` : label;
}

// While a batch (a bulk archive) runs, its keys' reports are collected by the
// batch instead of spoken, so N archives raise one notice. Each batch owns its
// key set: two overlapping batches never swallow each other's reports, and an
// archive of a key in NO batch is a single archive and speaks for itself.
interface Batch {
  keys: Set<string>;
  entries: { key: string; report: TeardownReport | undefined }[];
}
const batches = new Set<Batch>();

/** Route one archive's report: true when a batch holding `key` collected it. */
export function routeArchiveReport(key: string, report: TeardownReport | undefined): boolean {
  for (const b of batches) {
    if (b.keys.has(key)) {
      b.entries.push({ key, report });
      return true;
    }
  }
  return false;
}

// Notices whose "Remove anyway" request is in flight: a second click waits.
const removing = new Set<number>();

function removeAnyway(byKey: { key: string; paths: string[] }[], noun: string, id: number): void {
  if (removing.has(id)) return;
  removing.add(id);
  void Promise.all(byKey.map((b) => removeTaskWorktrees(b.key, b.paths)))
    .then((rs) => {
      // The request was answered: the "Kept..." notice is stale either way.
      dismissNotification(id);
      const left = rs.flatMap((r) => r.kept);
      if (left.length > 0) {
        notify({ title: "Could not remove", detail: keptReasonText(left[0]), tone: "error" });
        return;
      }
      // A `gone` worktree is already what the reader asked for.
      const n = rs.reduce((t, r) => t + (r.removed?.length ?? 0) + (r.gone?.length ?? 0), 0);
      notify({ title: `Removed ${plural(n, "worktree", "worktrees")}`, tone: "info" });
    })
    .catch(() => notify({ title: `Could not remove the ${noun}`, tone: "error" }))
    .finally(() => removing.delete(id));
}

function raise(n: KeptNotice & { byKey: { key: string; paths: string[] }[] }, noun: string): void {
  const id: number = notify({
    title: n.title,
    detail: n.detail,
    tier: "attention",
    action: { label: "Remove anyway", onClick: () => removeAnyway(n.byKey, noun, id) },
  });
}

/** Start collecting kept-worktree reports for `keys`; `end()` raises the one
 *  notice for THIS batch (and returns it), and its "Remove anyway" targets this batch's keys. */
export function beginArchiveBatch(keys: readonly string[]): { end: () => BatchKeptNotice | null } {
  const mine: Batch = { keys: new Set(keys), entries: [] };
  batches.add(mine);
  return {
    end: () => {
      batches.delete(mine);
      const n = batchKeptNotice(mine.entries);
      if (n) raise(n, "worktrees");
      return n;
    },
  };
}

/** Mounted once at the app root: renders nothing. */
export function TeardownNotifier(): null {
  useEffect(
    () =>
      onTaskArchived((key, res) => {
        if (routeArchiveReport(key, res.teardown)) return;
        const n = keptWorktreeNotice(key, res.teardown);
        if (n) raise({ ...n, byKey: [{ key, paths: n.paths }] }, "worktree");
      }),
    [],
  );
  return null;
}
