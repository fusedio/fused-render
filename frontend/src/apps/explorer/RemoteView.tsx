// The explorer page at an `s3://bucket/key` LOCATION (platform/lib/remote.ts).
//
// App.tsx mounts this in place of StatView whenever the decoded location is
// remote, so nothing below StatView (Listing, the local stat, recents, the
// favicon probe, EmbedStrip) ever sees an `s3://` string. The crumb bar is the
// ordinary BreadcrumbBar, which draws S3 crumbs for a remote location.
//
// Three bodies, by the server's stat:
//   - a prefix (`kind: "dir"`) → the explorer's own folder view (Listing), the
//     same component a local folder gets: it lists, searches, completes and
//     resolves typed addresses from the bucket index (listing/source.ts), with
//     every write and the side pane switched off (Listing's `remote`);
//   - a cached file → the EXISTING viewer (Preview) on the cached local copy,
//     with `remoteUrl` set so the viewer keeps every local-file affordance
//     (rename, delete, reveal, share, splits, sidebar, app rows) out of reach
//     and its menus speak the s3 address instead;
//   - an uncached file with modes (`stat.templates`, D1492) → the same viewer
//     on the `s3://` address, with its mode switcher: a mode whose template
//     streams the object (its pyproject.toml `[tool.fused-render.template]
//     stream`) frames the address and reads only what it shows, under a
//     "Streaming from S3" bar with Download; every other mode shows the
//     download card in its pane, and opens on the cached copy once it lands;
//   - an uncached file with no modes → the download card: at once up to
//     `warn_bytes`, behind a notice above it (shown every time), with
//     progress and Cancel.
// The address bar, tab title and history stay on the `s3://` address
// throughout; only Preview's iframe ever receives the local cache path, and
// only a streaming template's iframe receives the `s3://` address.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { canonFsPath } from "@platform/lib/path-url";
import { IS_PREVIEW, navHintIsDir, navigate, navigateUrl } from "@platform/lib/router";
import { basename, formatSize } from "@platform/lib/format";
import { useDocumentTitle, useNavReach } from "@platform/lib/hooks";
import { goBack } from "@platform/lib/nav-history";
import { statPath, type StatResult } from "@platform/lib/api";
import {
  RemoteApiError,
  parseRemoteLocation,
  remoteFetch,
  remoteFetchCancel,
  remoteFetchStatus,
  remoteStat,
  type RemoteStat,
} from "@platform/lib/remote";
import { notify } from "@platform/lib/notifications";
import { isSidebarMode } from "@platform/lib/mode-visibility";
import { BreadcrumbBar } from "@apps/explorer/Breadcrumb";
import Listing from "@apps/explorer/Listing";
import Preview, { type RemoteContent } from "@apps/explorer/Preview";
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
  autoStart = false,
  onStarted,
  onCancelled,
}: {
  stat: RemoteStat;
  /** The download finished: the caller re-stats and renders the cache. */
  onDone: () => void;
  /** Start (or join) the download at once, even past `warn_bytes`: the
   *  reader already asked for it, in this card or another mode's. */
  autoStart?: boolean;
  /** The download was asked for (Download, or an automatic start). */
  onStarted?: () => void;
  /** The server reports the download cancelled. */
  onCancelled?: () => void;
}) {
  const big = stat.size === null || stat.size > stat.warn_bytes;
  const confirmFirst = big && !autoStart;
  const [phase, setPhase] = useState<FetchPhase>(() => (confirmFirst ? { kind: "confirm" } : { kind: "starting" }));
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
          else if (s.state === "cancelled") {
            setPhase({ kind: "cancelled" });
            onCancelled?.();
          }
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
    onStarted?.();
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

  // A file under the threshold starts at once, and so does a download the
  // reader already asked for (`autoStart`): a running one is joined.
  const startedRef = useRef(false);
  useEffect(() => {
    if (confirmFirst || startedRef.current) return;
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

// -- uncached file: the viewer on the address, one pane per mode ---------------

/** An uncached object with modes (`stat.templates`, D1492): the ordinary
 *  viewer, its mode switcher included, on the `s3://` address. A mode whose
 *  template streams the object gets the address as `_file` and reads only the
 *  byte ranges it shows, through a URL the server signs (`fused.s3.sign` /
 *  `fused_s3`). Any other mode shows the download card in its pane; once the
 *  copy lands the page re-stats and opens it, on the same `_mode`.
 *
 *  Once a download is asked for — a pane's Download or automatic start, or
 *  the bar's Download (the duckdb grid points at it on a sort that would
 *  scan most of the file) — the whole file is downloading: every mode,
 *  streaming ones included, shows the card, and a card mounted by a mode
 *  switch joins the running download instead of asking again. So the shown
 *  pane is always the one watching when the copy lands. A cancelled
 *  download puts the modes back. */
function RemoteUncachedFile({
  stat,
  onRenderedTitle,
  onDone,
}: {
  stat: RemoteStat;
  onRenderedTitle: (t: string | null) => void;
  onDone: () => void;
}) {
  const [downloading, setDownloading] = useState(false);
  const templates = stat.templates ?? [];
  const streams = (mode: string) => !downloading && templates.some((t) => t.mode === mode && t.stream);
  const remoteContent: RemoteContent = {
    pane: (mode) =>
      streams(mode) ? null : (
        <RemoteDownload
          stat={stat}
          onDone={onDone}
          autoStart={downloading}
          onStarted={() => setDownloading(true)}
          onCancelled={() => setDownloading(false)}
        />
      ),
    banner: (mode) =>
      streams(mode) ? (
        <div className="listing-snapshot-banner">
          Streaming from S3: only the parts you view are read
          {stat.size !== null ? ` (${formatSize(stat.size)} in the bucket)` : ""}.
          <button type="button" className="listing-snapshot-back" onClick={() => setDownloading(true)}>
            Download
          </button>
        </div>
      ) : null,
  };
  const asStat: StatResult = {
    path: stat.url,
    name: stat.name,
    is_dir: false,
    size: stat.size,
    mtime: stat.mtime,
    writable: false,
    templates,
  };
  return (
    <Preview
      fsPath={stat.url}
      stat={asStat}
      remoteUrl={stat.url}
      remoteContent={remoteContent}
      onRenderedTitle={onRenderedTitle}
      actionsInTopbar
    />
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
    // `location`, not the stat's url: the folder is addressed exactly as the
    // address bar spells it, so its rows, crumbs and saved sort agree.
    content = <Listing fsPath={location} barChrome />;
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
  } else if ((state.stat.templates ?? []).some((t) => !isSidebarMode(t.mode) && !t.conditional)) {
    // Preview drops companion modes (claude, git) for a remote file, so a
    // file bound only to one (a user's ungated `claude` fork) gets the
    // download card below, not a viewer with nothing to show.
    content = (
      <RemoteUncachedFile
        key={state.stat.url}
        stat={state.stat}
        onRenderedTitle={setRenderedTitle}
        onDone={reload}
      />
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

