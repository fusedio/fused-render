// The plain (non-search) directory listing: the /api/fs/list fetch, the
// Load-more pagination for truncated listings, the WebSocket dir watch that
// drives refreshes, and the "new row" tint cue.
import { useEffect, useRef, useState } from "react";
import { clearListPrefetch, listDir, prefetchListDir } from "@platform/lib/api";
import { appearedKeys } from "@platform/lib/flip";
import { pushToast } from "@platform/lib/toast";
import { ROW_NEW_MS, type ListingState } from "@apps/explorer/listing/types";

export function useDirListing(fsPath: string) {
  const [state, setState] = useState<ListingState>({ status: "loading" });
  const [refresh, setRefresh] = useState(0); // bumped by the dir watch socket
  // loadMore captures the refresh generation it started in; a dir-watch refresh
  // on the SAME path (App keys StatView on epoch+fsPath, so cross-directory
  // merges can't happen, but a same-path re-fetch can) resets the listing while
  // a cursored fetch is pending — the stale page must be discarded, not merged
  // into the refreshed listing. Ref so the async callback reads the LATEST
  // generation, not the one captured when loadMore was defined.
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  // A Load more fetch (next page of a truncated listing) is in flight.
  const [loadingMore, setLoadingMore] = useState(false);
  // Set by loadMore: an appended page is the user's own gesture, and tinting
  // 250 rows they just asked for is noise, not a cue.
  const skipNewCue = useRef(false);

  useEffect(() => {
    let alive = true;
    // A fresh fetch (navigation or dir-watch refresh) resets any accumulated
    // Load more pages: the new listing replaces the array wholesale.
    setLoadingMore(false);
    // Initial mount goes through the prefetch cache so a listing kicked off by
    // the loading scaffold (in parallel with stat) is reused when the real
    // preview remounts this component for the same path — no duplicate request.
    // A dir-watch refresh (refresh > 0) must see live data, so it bypasses.
    (refresh === 0 ? prefetchListDir(fsPath) : listDir(fsPath)).then(
      (data) =>
        alive &&
        setState({
          status: "ok",
          entries: data.entries,
          truncated: !!data.truncated,
          cursor: data.cursor ?? null,
        }),
      (err: Error) => alive && setState({ status: "error", message: err.message })
    );
    return () => {
      alive = false;
    };
  }, [fsPath, refresh]);

  // Fetch the next page of a truncated S3-direct listing and APPEND it (dedupe
  // by name). The accumulated set is still sorted by the active column —
  // honest because the banner states the listing is partial; a global sort over
  // the WHOLE directory is impossible (we only ever hold fetched pages).
  const loadMore = () => {
    if (state.status !== "ok" || !state.cursor || loadingMore) return;
    const cursor = state.cursor;
    const gen = refresh; // discard the response if a refresh supersedes it
    setLoadingMore(true);
    skipNewCue.current = true; // an appended page isn't a dir-watch change
    listDir(fsPath, cursor).then(
      (data) => {
        if (refreshRef.current !== gen) return; // stale: a refresh replaced the listing
        setLoadingMore(false);
        setState((prev) => {
          if (prev.status !== "ok") return prev;
          const seen = new Set(prev.entries.map((e) => e.name));
          const merged = prev.entries.concat(
            data.entries.filter((e) => !seen.has(e.name))
          );
          return {
            status: "ok",
            entries: merged,
            truncated: !!data.truncated,
            cursor: data.cursor ?? null,
          };
        });
      },
      (err: Error) => {
        if (refreshRef.current !== gen) return; // stale: the fetch effect reset state
        setLoadingMore(false);
        pushToast({ msg: err.message, tone: "error" });
      }
    );
  };

  // WebSocket watch on the listed directory (LS-1); WS not SSE per D74 (SSE
  // pinned one of Chrome's 6 HTTP/1.1 sockets per view). A directory's mtime
  // changes on create/delete/rename of entries (not on child content changes
  // — LS-2, accepted). Closed on unmount = navigating away (LS-3). On change,
  // debounce 300 ms then re-fetch; sort params live in URL + state, so a
  // refetch preserves them.
  useEffect(() => {
    let sock: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss://" : "ws://";
      sock = new WebSocket(proto + location.host + "/api/fs/events?path=" + encodeURIComponent(fsPath));
      sock.onmessage = (ev) => {
        let data;
        try {
          data = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (data.keepalive) return;
        // AN EXTERNAL WRITER CHANGED THIS FOLDER — drop every cached listing.
        //
        // api.ts's own mutation wrappers invalidate the prefetch cache themselves,
        // so this is for changes made by something else: Claude, an editor, a git
        // checkout. Without it this listing refreshed correctly (a refresh
        // bypasses the cache) while a fresh MOUNT of the same folder within the 5s
        // TTL still painted the pre-change contents.
        //
        // THE SCOPE IS NARROW, and no comment should imply otherwise: this socket
        // watches only `fsPath`, and only while this component is mounted. A write
        // to any other folder is invisible here, and a template view of a FILE
        // mounts no listing at all — writes from inside a preview iframe are
        // covered instead by window._fusedFsChanged (installed in main.tsx).
        //
        // Before the debounce, not inside it: the cache should be dead the moment
        // we know it is wrong, whether or not this listing goes on to refetch.
        clearListPrefetch();
        if (timer !== null) clearTimeout(timer);
        timer = setTimeout(() => setRefresh((n) => n + 1), 300);
      };
      // WebSockets don't auto-reconnect the way EventSource did.
      sock.onclose = () => {
        if (!closed) retry = setTimeout(connect, 1000);
      };
    };
    connect();
    return () => {
      closed = true;
      if (retry !== null) clearTimeout(retry);
      if (timer !== null) clearTimeout(timer);
      sock?.close();
    };
  }, [fsPath]);

  // Dir-watch change cue (B5). A refresh that adds entries used to slot them in
  // silently — a file that just landed in the folder was indistinguishable from
  // one that had been there all along. Newly appeared rows carry `.row-new` for
  // ROW_NEW_MS (a tint that fades out; removals need no cue beyond the FLIP).
  // Names are the plain listing's row identity. The FIRST listing of a folder is
  // never "new" — appearedKeys returns nothing for a null previous list.
  const prevNamesRef = useRef<string[] | null>(null);
  const [newNames, setNewNames] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    if (state.status !== "ok") return;
    const names = state.entries.map((e) => e.name);
    const fresh = skipNewCue.current ? new Set<string>() : appearedKeys(prevNamesRef.current, names);
    skipNewCue.current = false;
    prevNamesRef.current = names;
    if (fresh.size === 0) return;
    setNewNames(fresh);
    const id = window.setTimeout(() => setNewNames(new Set()), ROW_NEW_MS);
    return () => window.clearTimeout(id);
  }, [state]);

  const refetch = () => setRefresh((n) => n + 1);

  return { state, refresh, refetch, loadMore, loadingMore, newNames };
}
