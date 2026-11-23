// Whether a path named like a `.fused` / `.fusedbot` package is one. A name
// alone cannot say: a git repo FOLDER called `x.fused` matches too. A
// navigation that knows what it opened carries a hint (navHintIsDir); any other
// entry (task door, typed URL, deeplink, reload) is settled by a stat.
import { useEffect, useState } from "react";
import { statPath } from "@platform/lib/api";
import { navHintIsDir } from "@platform/lib/router";

export type InstallVerdict = "install" | "listing" | "pending";

// `statIsDir` is null while the stat is in flight; a failed stat resolves to
// false, which keeps the name-based answer.
export function installVerdict(hint: boolean | null, statIsDir: boolean | null): InstallVerdict {
  const isDir = hint ?? statIsDir;
  if (isDir === null) return "pending";
  return isDir ? "listing" : "install";
}

// Stats `fsPath` while `active` (its name matched) and no hint is present.
// Returns null until the answer for THIS path is in; abortable on path change.
export function useStatIsDir(fsPath: string | null, active: boolean): boolean | null {
  const [seen, setSeen] = useState<{ path: string; isDir: boolean } | null>(null);
  useEffect(() => {
    if (!fsPath || !active || navHintIsDir() !== null) return;
    const ac = new AbortController();
    statPath(fsPath, ac.signal).then(
      (s) => setSeen({ path: fsPath, isDir: s.is_dir }),
      () => {
        if (!ac.signal.aborted) setSeen({ path: fsPath, isDir: false });
      },
    );
    return () => ac.abort();
  }, [fsPath, active]);
  return seen && seen.path === fsPath ? seen.isDir : null;
}
