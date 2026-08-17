// Per-file session restore (LSN-*, SPEC §21). A viewed file remembers its last
// URL query in its <file>.json sidecar; opening it with a bare URL replays that
// query, while opening it with params already present lets those params win.
//
// NOT EVERY PARAM, since D326: `_side` is stripped at both ends of this — see
// lib/session-params, which is where that rule and its reasons live.
import { useEffect, useRef, useState } from "react";

import { getSession, putSession } from "@platform/lib/api";
import { IS_EMBED, replaceSearch } from "@platform/lib/router";
import { restoredSearch, stripSessionParams } from "@platform/lib/session-params";

function stripQ(): string {
  return location.search.replace(/^\?/, "");
}


// Restore-on-open (LSN-4/5/9). Returns "ready" once the restore decision is
// made so the caller can hold the preview until the URL is settled (no param
// flash). Non-empty query wins; embed panes and directories opt out. `isDir`
// and `writable` are null until the stat resolves — treated as "not a
// restorable file yet", so nothing fires against a path whose kind/writability
// is still unknown.
export function useSessionRestore(
  fsPath: string,
  isDir: boolean | null,
  writable: boolean | null,
): boolean {
  // Only a CONFIRMED, WRITABLE file (isDir === false && writable === true) with
  // an empty query attempts a restore. Everything else skips it and is ready
  // SYNCHRONOUSLY (LSN-9: no "Loading…" flash).
  //
  // writable === false is the key gate for read-only mounts: a non-writable
  // file can NEVER have had a sidecar written (the write is server-guarded in
  // _session_put — read-only mounts return skipped), so there is nothing to
  // replay. Without this gate, opening any file on a read-only S3/NFS mount
  // blocked the whole template on a cold GET /api/session (~1-2s+, seconds when
  // the mount is cold) that is GUARANTEED to return null — the read-only
  // "Loading…" stall. Skipping restore there lets the template mount the moment
  // stat resolves, with zero flash risk (there is no session to apply).
  //
  // The trade-off vs. speculatively firing the read before stat: a WRITABLE
  // file on a slow mount pays stat + session serially rather than overlapped.
  // That is rare (writable remote mounts are the exception; the default is
  // read-only) and its session read is what we actually need, so correctness +
  // not-touching-a-read-only-mount wins over shaving that uncommon case.
  //
  // "Empty query" means empty OF SESSION PARAMS (LSN-12): the omitted ones are
  // invisible to this layer in BOTH directions, so a url carrying nothing but
  // `?_side=off` — which is what a close click now writes, where it used to delete
  // the param — is a bare open and still gets its replay. Reading the raw query
  // here while `maybeSave` strips would have let one click silently switch the
  // file's restore off.
  const skip =
    IS_EMBED ||
    isDir !== false ||
    writable !== true ||
    stripSessionParams(stripQ()) !== "";
  const [restored, setRestored] = useState(false);
  useEffect(() => {
    if (skip) return;
    let alive = true;
    setRestored(false);
    getSession(fsPath).then(
      (r) => {
        if (!alive) return;
        // `_side` never comes back out of a sidecar, however it got in, and the
        // one on the LIVE url survives the write — `restoredSearch` holds both
        // halves and says why. An old sidecar whose only param was `_side`
        // restores nothing at all.
        const s = restoredSearch(r.lastSession?.search ?? "", stripQ());
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
    // fsPath + isDir + writable identify the open; the apply runs once per open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath, isDir, writable]);
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
export function useSessionTracking(
  fsPath: string,
  isDir: boolean | null,
  writable: boolean | null,
): void {
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    // Non-writable files (read-only mounts) never persist a session: the PUT is
    // server-skipped anyway, so tracking one just fires a wasted round-trip to a
    // (possibly cold/slow) mount on every param tweak. Opt out here, mirroring
    // useSessionRestore's writable gate.
    if (IS_EMBED || isDir !== false || writable !== true) return;
    const maybeSave = () => {
      // `_side` is dropped BEFORE the PUT, not left for the server to drop: a URL
      // whose only param is `_side` must read as a bare URL here, so opening the
      // sidebar on a file cannot be the thing that starts that file's session
      // (LSN-12). The server enforces the same rule for anything that reaches it
      // by another route (server/session.py).
      const search = stripSessionParams(stripQ());
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fsPath, isDir, writable]);
}
