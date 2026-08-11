// /local-models: what the Hugging Face cache holds on this machine.
//
// The cache is shared by everything that speaks huggingface_hub — a
// transformers import, a diffusers pipeline, an `hf download`, a page a user
// pasted in — and it is invisible: it fills up under ~/.cache with multi-GB
// checkpoints nothing on screen ever mentions. This page is the missing
// inventory: one row per cached repo, biggest first, with what it costs on
// disk and a click through to the folder in the explorer.
//
// Read-only by design (GET /api/local-models, routers/local_models.py). No
// delete button: evicting a blob out from under a half-loaded pipeline is a
// different feature with a different set of confirmations, and "show me what's
// there" is the whole ask here.
//
// Page chrome is the cc-* family (ClaudeArtifacts does the same) so the shell's
// non-explorer pages read as one surface; only the row layout is local
// (styles/local-models.css).
import { useEffect, useState } from "react";
import { getLocalModels, getLocalModelsStatus, type LocalModelRepo, type LocalModelsResult } from "@platform/lib/api";
import { formatSize, formatMtimeFull, timeAgo } from "@platform/lib/format";
import { navigate, urlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

// Sidebar gate. Availability is "does the hub cache dir exist", which — unlike
// the Claude config bridge's install-shaped answer — CAN flip mid-session: the
// first model a user ever downloads creates it. So a confirmed `true` is cached
// for the session (the row must not blink out when the shell swaps sidebars),
// while a `false` is only cached for PROBE_TTL_MS and re-probed by the next
// mount after it lapses. That bounds the cost to roughly one isdir() a minute
// for the majority of machines that have no cache at all.
//
// The answer is PUBLISHED rather than just stored, because the two writers are
// not the mounted sidebar: a probe from another mount, and the page's own load
// (which knows the truth without a second request), both have to reach a
// sidebar that is already on screen. Without that, opening /local-models by URL
// on a machine whose cache appeared this session would update the cache and
// leave the entry missing until something remounted the sidebar.
const PROBE_TTL_MS = 60_000;
let cached: { available: boolean; at: number } | null = null;
const gateListeners = new Set<(available: boolean) => void>();

function publishAvailable(available: boolean) {
  cached = { available, at: Date.now() };
  for (const listener of gateListeners) listener(available);
}

export function useLocalModelsAvailable(): boolean {
  const [available, setAvailable] = useState(cached?.available ?? false);
  useEffect(() => {
    gateListeners.add(setAvailable);
    // An answer that landed between this render and this effect (another
    // mount's probe resolving) would otherwise be missed.
    if (cached) setAvailable(cached.available);
    if (!cached || (!cached.available && Date.now() - cached.at >= PROBE_TTL_MS)) {
      getLocalModelsStatus().then(
        (s) => publishAvailable(s.available),
        () => {
          // A failed probe is not a cached "no": leave the last known answer
          // (and the absent cache entry) alone so a transient fetch failure
          // neither hides a shown entry nor suppresses the next probe.
        },
      );
    }
    return () => {
      gateListeners.delete(setAvailable);
    };
  }, []);
  return available;
}

type Load =
  | { status: "loading" }
  | { status: "ok"; data: LocalModelsResult }
  | { status: "error"; message: string };

function RepoRow({ repo }: { repo: LocalModelRepo }) {
  const when = timeAgo(repo.mtime);
  // The folder opens in the explorer, but it stays a real <a href> so
  // middle-click and "copy link" behave (same contract as the bookmark cards).
  return (
    <a
      className="lm-row"
      href={urlForFsPath(repo.path)}
      title={repo.path}
      onClick={(e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
          return;
        e.preventDefault();
        navigate(repo.path, { isDir: true });
      }}
    >
      <span className="lm-row-main">
        <span className="lm-row-name">
          {repo.id} <span className="cc-pill">{repo.kind}</span>
        </span>
        <span className="lm-row-meta">
          {repo.files} {repo.files === 1 ? "file" : "files"}
          {repo.revisions > 1 ? ` · ${repo.revisions} revisions` : ""}
          {repo.refs.length ? ` · ${repo.refs.join(", ")}` : ""}
          {when ? ` · ${when}` : ""}
        </span>
      </span>
      <span className="lm-row-size" title={repo.mtime ? `Last changed ${formatMtimeFull(repo.mtime)}` : undefined}>
        {formatSize(repo.size)}
      </span>
    </a>
  );
}

export default function LocalModels() {
  const [load, setLoad] = useState<Load>({ status: "loading" });
  // Bumped by Refresh to re-run the scan in place. Scanning is a disk walk over
  // every blob, so it happens on mount and on an explicit Refresh — never on a
  // focus/return tick, which would re-walk tens of thousands of files every
  // time the user alt-tabbed back.
  const [reloadKey, setReloadKey] = useState(0);
  useEffect(() => {
    let alive = true;
    setLoad({ status: "loading" });
    getLocalModels().then(
      (data) => {
        if (!alive) return;
        // The page's own answer is authoritative for the sidebar gate: a cache
        // that exists (or has just appeared) shouldn't wait out the probe TTL,
        // and a sidebar already on screen hears this immediately.
        publishAvailable(data.exists);
        setLoad({ status: "ok", data });
      },
      (e: Error) => alive && setLoad({ status: "error", message: e.message }),
    );
    return () => {
      alive = false;
    };
  }, [reloadKey]);

  const data = load.status === "ok" ? load.data : null;
  const repos = data?.repos ?? [];

  return (
    <div className="cc-root">
      <main className="cc-main">
        <div className="cc-page-head">
          <div>
            <h2 className="cc-heading">Local models</h2>
            <div className="cc-caption cc-mono">
              {data
                ? `${data.cacheDir}${repos.length ? ` · ${repos.length} cached · ${formatSize(data.totalSize)}` : ""}`
                : "Hugging Face cache"}
            </div>
          </div>
          <button
            type="button"
            className="btn"
            onClick={() => setReloadKey((k) => k + 1)}
            disabled={load.status === "loading"}
          >
            {load.status === "loading" ? "Scanning…" : "Refresh"}
          </button>
        </div>
        {load.status === "error" && <ErrorBanner>{load.message}</ErrorBanner>}
        {load.status === "loading" && <p className="cc-empty">Reading the Hugging Face cache…</p>}
        {data &&
          (repos.length ? (
            <div className="lm-list">
              {repos.map((r) => (
                <RepoRow key={r.path} repo={r} />
              ))}
            </div>
          ) : (
            // Two different nothings: no cache dir at all (nothing has ever
            // pulled from the Hub) versus a cache that has been emptied. The
            // path itself is already in the caption above, so it isn't repeated
            // here.
            <p className="cc-empty">
              {data.exists
                ? "Nothing cached here yet."
                : "No Hugging Face cache on this machine — the first download from the Hub creates it."}
            </p>
          ))}
      </main>
    </div>
  );
}
