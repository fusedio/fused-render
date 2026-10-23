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
import { notify } from "@platform/lib/notifications";

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

/** Mounted once at the app root: renders nothing. */
export function TeardownNotifier(): null {
  useEffect(
    () =>
      onTaskArchived((key, res) => {
        const n = keptWorktreeNotice(key, res.teardown);
        if (!n) return;
        notify({
          title: n.title,
          detail: n.detail,
          tier: "attention",
          action: {
            label: "Remove anyway",
            onClick: () => {
              void removeTaskWorktrees(key, n.paths)
                .then((r) => {
                  if (r.kept.length > 0) {
                    notify({ title: "Could not remove", detail: keptReasonText(r.kept[0]), tone: "error" });
                  }
                })
                .catch(() => notify({ title: "Could not remove the worktree", tone: "error" }));
            },
          },
        });
      }),
    [],
  );
  return null;
}
