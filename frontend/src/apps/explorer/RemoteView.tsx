// The explorer page at an `s3://bucket/key` LOCATION (platform/lib/remote.ts).
//
// App.tsx mounts this in place of StatView whenever the decoded location is
// remote, so nothing below StatView (Listing, the local stat, recents, the
// favicon probe, EmbedStrip) ever sees an `s3://` string. The crumb bar is the
// ordinary BreadcrumbBar, which draws S3 crumbs for a remote location.
//
// Three bodies, by the server's stat:
//   - a prefix (`kind: "dir"`) → a read-only, one-level table (RemoteListing),
//     with a search box over the folder's whole subtree (RemoteFolderSearch.tsx);
//   - a cached file → the EXISTING viewer (Preview) on the cached local copy,
//     with `remoteUrl` set so the viewer keeps every local-file affordance
//     (rename, delete, reveal, share, splits, sidebar, app rows) out of reach
//     and its menus speak the s3 address instead;
//   - an uncached file one of its templates streams (`stream_templates`, a
//     `stream-s3` marker, D1484) → that template's page on the `s3://` address
//     itself, reading only what it shows; Download is a button above it;
//   - any other uncached file → download first: at once up to `warn_bytes`,
//     behind a notice above it (shown every time), with progress and Cancel.
// The address bar, tab title and history stay on the `s3://` address
// throughout; only Preview's iframe ever receives the local cache path, and
// only a streaming template's iframe receives the `s3://` address.
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { canonFsPath } from "@platform/lib/path-url";
import { IS_PREVIEW, navHintIsDir, navigate, navigateUrl, replaceSearch, spaLinkProps } from "@platform/lib/router";
import { basename, formatMtime, formatMtimeFull, formatSize } from "@platform/lib/format";
import { useDocumentTitle, useNavReach } from "@platform/lib/hooks";
import { goBack } from "@platform/lib/nav-history";
import { statPath, type StatResult } from "@platform/lib/api";
import {
  RemoteApiError,
  parseRemoteLocation,
  remoteFetch,
  remoteFetchCancel,
  remoteFetchStatus,
  remoteList,
  remoteStat,
  type RemoteEntry,
  type RemoteRankHit,
  type RemoteStat,
} from "@platform/lib/remote";
import { notify } from "@platform/lib/notifications";
import { substringMatch } from "@platform/lib/fuzzy";
import { iconForEntry } from "@platform/ui/FileIcons";
import { renderHighlightPath } from "@apps/explorer/listing/bits";
import { useRankedSearchEnabled } from "@apps/explorer/lib/ranked-search-pref";
import { RemoteSearchRow, remoteSearchCount, useRemoteFolderSearch } from "@apps/explorer/RemoteFolderSearch";
import { BreadcrumbBar } from "@apps/explorer/Breadcrumb";
import Preview from "@apps/explorer/Preview";
import { PreviewSideSlot } from "@apps/explorer/PreviewSidebar";

const POLL_MS = 500;
const SETTINGS_URL = "/preferences?s=canvases";

function asRemoteError(err: unknown): RemoteApiError {
  if (err instanceof RemoteApiError) return err;
  const message = err instanceof Error && err.message ? err.message : "Something went wrong";
  return new RemoteApiError("internal", message);
}

function isAbort(err: unknown): boolean {
  return err instanceof Error && err.name === "AbortError";
}

// `index_not_ready`: the fused-asset index is still loading server-side, which
// answers itself in a few seconds. Folder stat/list retry on this backoff
// before the page settles on the message; an abort (unmount, navigation)
// stops the wait and rejects with the AbortError.
const INDEX_RETRY_MS = [2000, 4000, 8000];

function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException("Aborted", "AbortError"));
    const t = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      window.clearTimeout(t);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

async function retryWhileIndexLoads<T>(run: () => Promise<T>, signal: AbortSignal): Promise<T> {
  for (let i = 0; ; i++) {
    try {
      return await run();
    } catch (err) {
      if (!(err instanceof RemoteApiError) || err.code !== "index_not_ready" || i >= INDEX_RETRY_MS.length) throw err;
      await abortableSleep(INDEX_RETRY_MS[i], signal);
    }
  }
}

/** The prefix one level up, for Cancel when there is no history to go back
 *  to (a pasted address opened in a fresh tab). The bucket root is its own
 *  parent. */
function parentLocation(loc: string): string {
  const p = parseRemoteLocation(loc);
  if (!p) return loc;
  const key = p.key.replace(/\/+$/, "");
  const i = key.lastIndexOf("/");
  return `s3://${p.bucket}/${i < 0 ? "" : key.slice(0, i + 1)}`;
}

/** A prefix URL for `remoteList`: always ends in "/" (the location in the
 *  address bar does not carry it — path-url.ts rootedFsPath). */
function asPrefix(url: string): string {
  return url.endsWith("/") ? url : url + "/";
}

// -- error body ---------------------------------------------------------------

function RemoteErrorBody({ error, onRetry }: { error: RemoteApiError; onRetry?: () => void }) {
  let title: string;
  let detail: string | null = null;
  let action: ReactNode = null;
  switch (error.code) {
    case "disabled":
      title = "The canvas integration is off";
      detail = "Turn on Canvases in Settings to open S3 addresses.";
      action = (
        <Button size="sm" variant="outline" onClick={() => void navigateUrl(SETTINGS_URL)}>
          Open Settings
        </Button>
      );
      break;
    case "not_public":
      title = "This file cannot be read";
      break;
    case "not_found":
      title = "Not found in the bucket";
      break;
    case "bad_url":
      title = "This is not a valid S3 address";
      detail = "Use s3://bucket/key.";
      break;
    case "network":
      title = "Could not reach the bucket";
      detail = error.message;
      break;
    case "unsupported_bucket": {
      title = "This bucket is not available";
      detail = "Only the fused-asset bucket can be opened for now.";
      action = (
        <Button size="sm" variant="outline" onClick={() => navigate("s3://fused-asset/", { isDir: true })}>
          Open s3://fused-asset
        </Button>
      );
      break;
    }
    case "index_not_ready":
      title = "The fused-asset index is still loading";
      detail = "Folders appear once it has loaded.";
      break;
    default:
      title = "Could not open this address";
      detail = error.message;
  }
  const retryable =
    error.code === "network" ||
    error.code === "index_not_ready" ||
    error.code === "internal";
  return (
    <div className="metadata-stack">
      <div className="metadata-card" role="alert">
        <p className="m-0 font-medium">{title}</p>
        {detail && <p className="m-0 pt-1 text-sm text-muted-foreground">{detail}</p>}
        {(action || (retryable && onRetry)) && (
          <div className="flex gap-2 pt-3">
            {action}
            {retryable && onRetry && (
              <Button size="sm" variant="outline" onClick={onRetry}>
                Retry
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// -- folder (prefix) listing --------------------------------------------------

const NO_HITS: RemoteRankHit[] = [];

/** The search query in the address bar (`?q=`), so Back from a hit lands on
 *  the folder still searching for it. */
function urlQuery(): string {
  return new URLSearchParams(location.search).get("q") || "";
}

function RemoteListing({ url }: { url: string }) {
  const [entries, setEntries] = useState<RemoteEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<RemoteApiError | null>(null);
  const [attempt, setAttempt] = useState(0);
  const ctl = useRef<AbortController | null>(null);

  const prefix = asPrefix(url);
  const [query, setQuery] = useState(urlQuery);
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const ranked = useRankedSearchEnabled();
  const search = useRemoteFolderSearch(prefix, query, ranked);
  const searching = search.status !== "idle";
  const hits = search.status === "done" ? search.hits : NO_HITS;

  // The query rides the current history entry (no new entry per keystroke).
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    if ((params.get("q") || "") === query) return;
    if (query) params.set("q", query);
    else params.delete("q");
    const qs = params.toString();
    replaceSearch(location.pathname + (qs ? "?" + qs : ""));
  }, [query]);
  // A new answer starts the keyboard back at its first row.
  useEffect(() => setSel(0), [hits]);
  useEffect(() => {
    document.querySelector("table.listing-table tr.row.selected")?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  const load = (from: string | null) => {
    ctl.current?.abort();
    const c = new AbortController();
    ctl.current = c;
    setLoading(true);
    setError(null);
    retryWhileIndexLoads(() => remoteList(prefix, { cursor: from, signal: c.signal }), c.signal).then(
      (r) => {
        if (c.signal.aborted) return;
        setEntries((prev) => (from ? [...prev, ...r.entries] : r.entries));
        setCursor(r.cursor);
        setLoading(false);
      },
      (err) => {
        if (c.signal.aborted || isAbort(err)) return;
        setError(asRemoteError(err));
        setLoading(false);
      },
    );
  };

  useEffect(() => {
    setEntries([]);
    setCursor(null);
    load(null);
    return () => ctl.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, attempt]);

  const onSearchKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      if (query) setQuery("");
      else e.currentTarget.blur();
    } else if (e.key === "ArrowDown" && hits.length) {
      e.preventDefault();
      setSel((i) => Math.min(i + 1, hits.length - 1));
    } else if (e.key === "ArrowUp" && hits.length) {
      e.preventDefault();
      setSel((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && hits[sel]) {
      e.preventDefault();
      navigate(hits[sel].url, { isDir: hits[sel].is_dir });
    }
  };

  const searchRow = (
    <RemoteSearchRow
      query={query}
      setQuery={setQuery}
      inputRef={inputRef}
      count={remoteSearchCount(search)}
      searching={search.status === "searching" || (search.status === "done" && search.held)}
      onKeyDown={onSearchKey}
    />
  );

  // A folder that cannot be listed has nothing to search either: the error
  // card alone, as before.
  if (error && entries.length === 0 && !searching) {
    return <RemoteErrorBody error={error} onRetry={() => setAttempt((n) => n + 1)} />;
  }

  // The status line a search shows in place of rows (null: rows, or nothing).
  let searchStatus: string | null = null;
  if (search.status === "searching") searchStatus = "Searching…";
  else if (search.status === "not_ready") searchStatus = "The fused-asset index is still loading. Results appear once it has loaded.";
  else if (search.status === "error") searchStatus = `Search failed: ${search.message}`;
  else if (search.status === "done" && hits.length === 0 && !search.held)
    searchStatus = `No matches for “${query.trim()}” in this folder or below it.`;

  const head = (
    <thead className={(searching ? hits.length : entries.length) === 0 ? "listing-head-empty" : undefined}>
      <tr>
        <th className="col-name">
          <span className="col-label">Name</span>
        </th>
        <th className="col-size">
          <span className="col-label">Size</span>
        </th>
        <th className="col-mtime">
          <span className="col-label">Modified</span>
        </th>
      </tr>
    </thead>
  );

  return (
    <div className="listing">
      {searchRow}
      <div className="listing-scroll">
        {searching ? (
          <table className="listing-table">
            {head}
            <tbody>
              {hits.map((hit, i) => {
                // The hit's path below this folder: what the row shows, with
                // the match marked (a query that is not one plain substring of
                // it renders unmarked, as the local search's rows do).
                const rel = hit.key.slice(prefixKey(prefix).length).replace(/\/+$/, "");
                const positions = substringMatch(query.trim(), rel)?.positions ?? [];
                const link = spaLinkProps(hit.url, { isDir: hit.is_dir });
                return (
                  <tr
                    key={hit.url}
                    className={"row" + (i === sel ? " selected" : "")}
                    title={hit.url}
                    onMouseMove={() => i !== sel && setSel(i)}
                    onClick={() => navigate(hit.url, { isDir: hit.is_dir })}
                  >
                    <td className="name">
                      <a
                        className="row-handle"
                        href={link.href}
                        onClick={(e) => {
                          e.stopPropagation();
                          link.onClick(e);
                        }}
                        style={{ color: "inherit", textDecoration: "none", cursor: "pointer" }}
                      >
                        <span className="icon">{iconForEntry(rel.split("/").pop() || rel, hit.is_dir)}</span>
                        <span className="search-path">{renderHighlightPath(rel, positions)}</span>
                      </a>
                    </td>
                    <td className="size">{hit.is_dir ? "" : formatSize(hit.size)}</td>
                    <td className="mtime" title={hit.mtime !== null ? formatMtimeFull(hit.mtime) : undefined}>
                      {hit.mtime !== null ? formatMtime(hit.mtime) : ""}
                    </td>
                  </tr>
                );
              })}
              {searchStatus !== null && (
                <tr>
                  <td colSpan={3} className={"status-message" + (search.status === "error" ? " error" : "")}>
                    {searchStatus}
                  </td>
                </tr>
              )}
              {search.status === "done" && search.truncated && !search.held && (
                <tr>
                  <td colSpan={3} className="status-message">
                    Showing the top {hits.length.toLocaleString()} matches. Type more to narrow them down.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        ) : (
          <table className="listing-table">
            {head}
            <tbody>
              {entries.map((entry) => {
                const link = spaLinkProps(entry.url, { isDir: entry.is_dir });
                return (
                  <tr
                    key={entry.url}
                    className="row"
                    onClick={() => navigate(entry.url, { isDir: entry.is_dir })}
                  >
                    <td className="name">
                      <a
                        className="row-handle"
                        href={link.href}
                        onClick={(e) => {
                          // The row's own click would navigate a second time.
                          e.stopPropagation();
                          link.onClick(e);
                        }}
                        style={{ color: "inherit", textDecoration: "none", cursor: "pointer" }}
                      >
                        <span className="icon">{iconForEntry(entry.name, entry.is_dir)}</span>
                        <span className="name-text">{entry.name}</span>
                      </a>
                    </td>
                    <td className="size">{entry.is_dir ? "" : formatSize(entry.size)}</td>
                    <td className="mtime" title={formatMtimeFull(entry.mtime)}>
                      {formatMtime(entry.mtime)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {!searching && loading && entries.length === 0 && (
          <div className="preview-resolving">
            <span className="mode-icon-spinner" />
            Listing…
          </div>
        )}
        {!searching && (cursor || (error && entries.length > 0)) && (
          <div className="listing-truncated">
            <div className="status-message">
              {error ? `Could not load more: ${error.message}` : `${entries.length} shown.`}
              <button
                type="button"
                className="listing-load-more"
                disabled={loading}
                onClick={() => load(cursor)}
              >
                {loading ? "Loading…" : error ? "Retry" : "Load more"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** The bucket key of a prefix address: `s3://b/a/b/` → `a/b/`, the root → "". */
function prefixKey(prefixUrl: string): string {
  return parseRemoteLocation(prefixUrl)?.key ?? "";
}

// -- uncached file: notice / download -----------------------------------------

type FetchPhase =
  | { kind: "confirm" }
  | { kind: "starting" }
  | { kind: "running"; id: string; done: number; total: number | null }
  // The server said done; the caller is re-statting to render the cache.
  | { kind: "finishing"; done: number; total: number | null }
  | { kind: "failed"; error: RemoteApiError }
  // The server no longer knows the job (it restarted mid-download).
  | { kind: "interrupted" }
  | { kind: "cancelled" };

function RemoteDownload({
  stat,
  onDone,
}: {
  stat: RemoteStat;
  /** The download finished: the caller re-stats and renders the cache. */
  onDone: () => void;
}) {
  const big = stat.size === null || stat.size > stat.warn_bytes;
  const [phase, setPhase] = useState<FetchPhase>(() => (big ? { kind: "confirm" } : { kind: "starting" }));
  // A cancel request is out (or was accepted and the poll has yet to see it).
  const [cancelling, setCancelling] = useState(false);
  const reach = useNavReach();
  const alive = useRef(true);
  const timer = useRef<number | undefined>(undefined);
  // Which download this card is still watching: a status reply that lands
  // after Cancel (or after a newer Retry) is for a watch that has ended.
  const watching = useRef<number>(0);
  const finish = (done: number, total: number | null) => {
    setPhase({ kind: "finishing", done, total });
    onDone();
  };
  // The re-stat after "done" came back still uncached (the copy went missing
  // between the two): say so instead of holding the progress card forever.
  // Runs on a NEW stat only, which is exactly the re-stat's answer.
  const firstStat = useRef(true);
  useEffect(() => {
    if (firstStat.current) {
      firstStat.current = false;
      return;
    }
    setPhase((p) =>
      p.kind === "finishing"
        ? { kind: "failed", error: new RemoteApiError("internal", "the downloaded copy is missing") }
        : p,
    );
  }, [stat]);
  useEffect(
    () => () => {
      // Leaving the page stops watching, not the download: the job carries on
      // server-side (and in the download manager), and coming back joins it.
      alive.current = false;
      window.clearTimeout(timer.current);
    },
    [],
  );

  const poll = (id: string, watch: number) => {
    timer.current = window.setTimeout(() => {
      remoteFetchStatus(id).then(
        (s) => {
          if (!alive.current || watching.current !== watch) return;
          if (s.state === "done") finish(s.total ?? s.done, s.total ?? stat.size);
          else if (s.state === "cancelled") setPhase({ kind: "cancelled" });
          else if (s.state === "error")
            setPhase({
              kind: "failed",
              error: new RemoteApiError(s.code ?? "internal", s.error || "The download failed"),
            });
          else {
            setPhase({ kind: "running", id, done: s.done, total: s.total ?? stat.size });
            poll(id, watch);
          }
        },
        (err) => {
          if (!alive.current || watching.current !== watch) return;
          const e = asRemoteError(err);
          // `unknown_download` from the STATUS route means the job id is
          // unknown (the app restarted) — the object was there when the
          // download started. `not_found` is the older server's spelling.
          setPhase(
            e.code === "unknown_download" || e.code === "not_found"
              ? { kind: "interrupted" }
              : { kind: "failed", error: e },
          );
        },
      );
    }, POLL_MS);
  };

  const start = () => {
    const watch = ++watching.current;
    window.clearTimeout(timer.current);
    setCancelling(false);
    setPhase({ kind: "starting" });
    remoteFetch(stat.url).then(
      (r) => {
        if (!alive.current || watching.current !== watch) return;
        if (r.state === "done") {
          finish(stat.size ?? 0, stat.size);
          return;
        }
        setPhase({ kind: "running", id: r.id, done: 0, total: stat.size });
        poll(r.id, watch);
      },
      (err) =>
        alive.current && watching.current === watch && setPhase({ kind: "failed", error: asRemoteError(err) }),
    );
  };

  // A file under the threshold starts at once.
  const startedRef = useRef(false);
  useEffect(() => {
    if (big || startedRef.current) return;
    startedRef.current = true;
    start();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Cancel only ASKS: the poll keeps running and is what reports the outcome
  // (the server's "cancelled" — or "done", if the download won the race). A
  // refused request says so and leaves the card watching the download.
  const cancel = (id: string) => {
    const watch = watching.current;
    setCancelling(true);
    remoteFetchCancel(id).catch((err) => {
      if (!alive.current || watching.current !== watch) return;
      setCancelling(false);
      notify({ title: `Could not cancel the download: ${asRemoteError(err).message}`, tone: "error" });
    });
  };

  const leave = () => {
    if (reach.back) goBack();
    else navigate(parentLocation(stat.url), { isDir: true });
  };

  let body: ReactNode;
  if (phase.kind === "confirm") {
    body = (
      <>
        <p className="m-0 font-medium">{stat.name}</p>
        <p className="m-0 pt-1 text-sm text-muted-foreground">
          {`This file is ${formatSize(stat.size)} and will be downloaded to view.`}
        </p>
        <div className="flex gap-2 pt-3">
          <Button size="sm" onClick={start}>
            Download
          </Button>
          <Button size="sm" variant="outline" onClick={leave}>
            Cancel
          </Button>
        </div>
      </>
    );
  } else if (phase.kind === "starting" || phase.kind === "running" || phase.kind === "finishing") {
    const done = phase.kind === "starting" ? 0 : phase.done;
    const total = phase.kind === "starting" ? stat.size : phase.total;
    const pct = total ? Math.min(100, Math.round((done / total) * 100)) : null;
    body = (
      <>
        <p className="m-0 font-medium">Downloading {stat.name}</p>
        <p className="m-0 pt-1 text-sm text-muted-foreground" style={{ fontVariantNumeric: "tabular-nums" }}>
          {total ? `${formatSize(done)} of ${formatSize(total)}` : formatSize(done)}
        </p>
        <div className="setup-progress-bar" style={{ width: "100%", marginTop: 10 }}>
          <span className="setup-progress-fill" style={{ width: `${pct ?? 0}%` }} />
        </div>
        <div className="flex gap-2 pt-3">
          <Button
            size="sm"
            variant="outline"
            disabled={phase.kind !== "running" || cancelling}
            onClick={() => phase.kind === "running" && !cancelling && cancel(phase.id)}
          >
            {cancelling ? "Cancelling…" : "Cancel"}
          </Button>
        </div>
      </>
    );
  } else {
    const message =
      phase.kind === "cancelled"
        ? "Download cancelled."
        : phase.kind === "interrupted"
          ? "The download was interrupted."
          : phase.error.code === "not_public"
          ? "This file cannot be read."
          : phase.error.code === "not_found"
            ? "Not found in the bucket."
            : `The download failed: ${phase.error.message}`;
    body = (
      <>
        <p className="m-0 font-medium">{stat.name}</p>
        <p className="m-0 pt-1 text-sm text-muted-foreground">{message}</p>
        <div className="flex gap-2 pt-3">
          <Button size="sm" onClick={start}>
            Retry
          </Button>
          <Button size="sm" variant="outline" onClick={leave}>
            Back
          </Button>
        </div>
      </>
    );
  }
  return (
    <div className="metadata-stack">
      <div className="metadata-card">{body}</div>
    </div>
  );
}

// -- cached file: the existing viewer on the cache copy -----------------------

function RemoteCachedFile({
  stat,
  onRenderedTitle,
  onReload,
}: {
  stat: RemoteStat;
  onRenderedTitle: (t: string | null) => void;
  onReload: () => void;
}) {
  const localPath = canonFsPath(stat.path as string);
  const [local, setLocal] = useState<
    { status: "loading" } | { status: "ok"; stat: StatResult } | { status: "error"; message: string }
  >({ status: "loading" });
  useEffect(() => {
    const c = new AbortController();
    setLocal({ status: "loading" });
    statPath(localPath, c.signal).then(
      (s) => !c.signal.aborted && setLocal({ status: "ok", stat: s }),
      (err: Error) => !c.signal.aborted && !isAbort(err) && setLocal({ status: "error", message: err.message }),
    );
    return () => c.abort();
  }, [localPath]);
  if (local.status === "loading") {
    return (
      <div className="preview-body">
        <div className="preview-resolving">
          <span className="mode-icon-spinner" />
          Loading…
        </div>
      </div>
    );
  }
  if (local.status === "error") {
    // The cache copy vanished between the two stats (evicted, or removed by
    // hand): re-stat the remote, which reports it uncached and downloads.
    return (
      <div className="metadata-stack">
        <div className="metadata-card" role="alert">
          <p className="m-0 font-medium">Could not open the downloaded copy</p>
          <p className="m-0 pt-1 text-sm text-muted-foreground">{local.message}</p>
          <div className="flex gap-2 pt-3">
            <Button size="sm" variant="outline" onClick={onReload}>
              Retry
            </Button>
          </div>
        </div>
      </div>
    );
  }
  return (
    <Preview
      fsPath={localPath}
      stat={local.stat}
      remoteUrl={stat.url}
      onRenderedTitle={onRenderedTitle}
      actionsInTopbar
      onReload={onReload}
    />
  );
}

// -- streamed file: the template reads the bucket itself ------------------------

/** A file whose template streams it (`stat.stream_templates`, a `stream-s3`
 *  marker in the template folder, D1484): the template's page gets the
 *  `s3://` address as `_file` and reads only the byte ranges it shows, through
 *  a URL the server signs (`fused.s3.sign` / `fused_s3`). No download, no
 *  size notice. Download stays one click away for a file the viewer will not
 *  stream (the duckdb grid says so for a sort that would scan most of the file). */
function RemoteStreamFile({ stat, onDownload }: { stat: RemoteStat; onDownload: () => void }) {
  const entry = (stat.stream_templates ?? [])[0];
  if (!entry || !entry.path) return null;
  const src = `/render?path=${encodeURIComponent(entry.path)}&_file=${encodeURIComponent(stat.url)}`;
  return (
    <div className="preview-body">
      <div className="preview-content-stack">
        <div className="listing-snapshot-banner">
          Streaming from S3: only the parts you view are read
          {stat.size !== null ? ` (${formatSize(stat.size)} in the bucket)` : ""}.
          <button type="button" className="listing-snapshot-back" onClick={onDownload}>
            Download
          </button>
        </div>
        <div className="preview-frames">
          <iframe key={src} className="preview-frame is-shown" src={src} title={stat.name} />
        </div>
      </div>
    </div>
  );
}

// -- the page -----------------------------------------------------------------

type RemoteState =
  | { status: "loading" }
  | { status: "ok"; stat: RemoteStat }
  | { status: "error"; error: RemoteApiError };

export function RemoteView({ location, home }: { location: string; home: string }) {
  const [reloadKey, setReloadKey] = useState(0);
  const [state, setState] = useState<RemoteState>({ status: "loading" });
  const [renderedTitle, setRenderedTitle] = useState<string | null>(null);
  // The streamed view's Download: the download card instead, for this address.
  const [downloadAnyway, setDownloadAnyway] = useState(false);
  // Reached as a FOLDER (a folder row, an ancestor crumb, Back to one: the
  // `isDir` nav hint in history.state, read once at mount like StatView's):
  // stat the prefix spelling, which the server answers from the index alone —
  // no live lookup, and an address that is both an object and a prefix opens
  // as the folder it was clicked as. No hint (pasted, reloaded, bookmarked):
  // the address as is, and the server decides.
  const [statUrl] = useState(() => (navHintIsDir() === true ? asPrefix(location) : location));
  useEffect(() => {
    const c = new AbortController();
    // A re-stat after a download keeps the progress card up until the answer
    // lands instead of flashing the loading state between the two.
    if (reloadKey === 0) setState({ status: "loading" });
    retryWhileIndexLoads(() => remoteStat(statUrl, { signal: c.signal }), c.signal).then(
      (stat) => !c.signal.aborted && setState({ status: "ok", stat }),
      (err) => !c.signal.aborted && !isAbort(err) && setState({ status: "error", error: asRemoteError(err) }),
    );
    return () => c.abort();
  }, [statUrl, reloadKey]);
  const reload = () => setReloadKey((k) => k + 1);
  // The error card's Retry: show the loading state for the re-stat (it can
  // take the whole index backoff), unlike the post-download reload above.
  const retry = () => {
    setState({ status: "loading" });
    reload();
  };

  const name = state.status === "ok" && state.stat.name ? state.stat.name : basename(location);
  useDocumentTitle(renderedTitle || name);

  let content: ReactNode;
  if (state.status === "loading") {
    content = (
      <div className="preview-body">
        <div className="preview-resolving">
          <span className="mode-icon-spinner" />
          Loading…
        </div>
      </div>
    );
  } else if (state.status === "error") {
    content = <RemoteErrorBody error={state.error} onRetry={retry} />;
  } else if (state.stat.kind === "dir") {
    content = <RemoteListing url={state.stat.url} />;
  } else if (state.stat.cached && state.stat.path) {
    content = (
      <RemoteCachedFile
        // `reloadKey` too: a reload (Retry, the registry fix's onReload) keeps
        // the same cache path and must still re-stat it and remount the viewer.
        key={state.stat.path + ":" + reloadKey}
        stat={state.stat}
        onRenderedTitle={setRenderedTitle}
        onReload={reload}
      />
    );
  } else if (IS_PREVIEW) {
    // A thumbnail (a bookmark card's peek) never starts a download: name and
    // size only. Opening the address itself is what fetches.
    content = (
      <div className="metadata-stack">
        <div className="metadata-card">
          <p className="m-0 font-medium">{state.stat.name}</p>
          <p className="m-0 pt-1 text-sm text-muted-foreground">
            {`${formatSize(state.stat.size)}, not downloaded yet`}
          </p>
        </div>
      </div>
    );
  } else if ((state.stat.stream_templates ?? []).length > 0 && !downloadAnyway) {
    content = (
      <RemoteStreamFile key={state.stat.url} stat={state.stat} onDownload={() => setDownloadAnyway(true)} />
    );
  } else {
    content = <RemoteDownload key={state.stat.url} stat={state.stat} onDone={reload} />;
  }
  // Same column structure as StatView, so the crumb bar and a file's topbar
  // actions land where they do for a local location. No EmbedStrip (its
  // Clone / back-to-explorer verbs are about a local app file).
  return (
    <div className="stat-split">
      <div className="stat-main">
        <BreadcrumbBar fsPath={location} home={home} renderedTitle={renderedTitle} />
        <div id="content">{content}</div>
      </div>
      <PreviewSideSlot />
    </div>
  );
}

