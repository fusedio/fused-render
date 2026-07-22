// Per-file session restore (LSN-*, SPEC §21). A viewed file remembers its last
// URL query in its <file>.json sidecar; opening it with a bare URL replays that
// query, while opening it with params already present lets those params win.
import { useEffect, useRef, useState } from "react";

import { getSession, putSession } from "./api";
import { IS_EMBED, replaceSearch } from "./router";

function stripQ(): string {
  return location.search.replace(/^\?/, "");
}

// Restore-on-open (LSN-4/5/9). Returns "ready" once the restore decision is
// made so the caller can hold the preview until the URL is settled (no param
// flash). Non-empty query wins; embed panes and directories opt out. `isDir`
// is null until the stat resolves — treated as "not a file yet", so nothing
// fires against a path whose kind is still unknown.
export function useSessionRestore(fsPath: string, isDir: boolean | null): boolean {
  // Only a CONFIRMED file (isDir === false) with an empty query attempts a
  // restore. Everything else — embed pane, directory, a not-yet-stat'd open
  // (isDir null), or any URL already carrying a query — skips it and is ready
  // SYNCHRONOUSLY on every render (LSN-9: no "Loading…" flash, and it flips the
  // instant the stat resolves the path to a bare-query file). The bare-query
  // file open is the only one that holds, tracked by `restored`.
  const skip = IS_EMBED || isDir !== false || stripQ() !== "";
  const [restored, setRestored] = useState(false);
  useEffect(() => {
    if (skip) return;
    let alive = true;
    setRestored(false);
    getSession(fsPath).then(
      (r) => {
        if (!alive) return;
        const s = r.lastSession?.search;
        if (s) replaceSearch(location.pathname + "?" + s);
        setRestored(true);
      },
      () => {
        if (alive) setRestored(true); // sidecar read failure -> render bare
      },
    );
    return () => {
      alive = false;
    };
    // fsPath + isDir identify the open; restore runs once per open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath, isDir]);
  return skip || restored;
}

// Track-on-change (LSN-3/10). Debounced fire-and-forget PUT of the current
// query. Empty queries are skipped here; the _mode-only-vs-qualifying gate
// (LSN-3: _mode alone never STARTS a session but updates one once it exists)
// lives server-side in _session_put, which is the authority — it reads the
// sidecar to know whether a lastSession already exists. Only a CONFIRMED file
// tracks: embed panes, directories, and not-yet-stat'd opens (isDir null) opt
// out, so a directory bookmark's params never PUT /api/session (LSN-6; the
// server 404s a directory path anyway).
export function useSessionTracking(fsPath: string, isDir: boolean | null): void {
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (IS_EMBED || isDir !== false) return;
    const maybeSave = () => {
      const search = stripQ();
      if (search === "") return; // nothing to record for a bare url
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        void putSession(fsPath, search);
      }, 400);
    };
    maybeSave(); // capture the as-restored/opened state
    window.addEventListener("fused:urlchange", maybeSave);
    window.addEventListener("popstate", maybeSave);
    return () => {
      window.removeEventListener("fused:urlchange", maybeSave);
      window.removeEventListener("popstate", maybeSave);
      window.clearTimeout(timer.current);
    };
  }, [fsPath, isDir]);
}
