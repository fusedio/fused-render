// /local-models: what the Hugging Face cache holds on this machine, and the
// deletions that free it.
//
// The cache is shared by everything that speaks huggingface_hub — a
// transformers import, a diffusers pipeline, an `hf download`, a page a user
// pasted in — and it is invisible: it fills up under ~/.cache with multi-GB
// checkpoints nothing on screen ever mentions. This page is the missing
// inventory: one row per cached repo, biggest first, with what it costs on
// disk and a click through to the folder in the explorer.
//
// It manages that cache too (D247), in three widening steps — a repo, one
// revision of a repo, or a prune of everything unread for N days. Every one of
// them names its targets in a confirmation the user reads first, and the
// dangerous arithmetic (which blobs a revision actually owns) lives on the
// server, where the filesystem is.
//
// Page chrome is the cc-* family (ClaudeArtifacts does the same) so the shell's
// non-explorer pages read as one surface; the row, drawer and prune list are
// local (styles/local-models.css).
import { useEffect, useState } from "react";
import {
  deleteLocalModels,
  getLocalModelRevisions,
  getLocalModels,
  getLocalModelsStatus,
  type LocalModelDeleteTarget,
  type LocalModelRepo,
  type LocalModelRevision,
  type LocalModelsResult,
} from "@platform/lib/api";
import { formatSize, formatMtimeFull, timeAgo } from "@platform/lib/format";
import { navigate, urlForFsPath } from "@platform/lib/router";
import { pushToast } from "@platform/lib/toast";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Modal } from "@platform/ui/modal/Modal";

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
// leave the entry missing until something remounted the sidebar. Deleting the
// last repo publishes too — the cache DIRECTORY survives an empty cache, so the
// entry stays, which is correct: the page still has a true thing to say.
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

// Prune thresholds, in days. Deliberately coarse and deliberately not
// "1 week": the shortest offer is a month because the cost of pruning a model
// you are about to use again is a multi-GB re-download.
const PRUNE_CHOICES = [30, 90, 180, 365];
const DEFAULT_PRUNE_DAYS = 90;

type Load =
  | { status: "loading" }
  | { status: "ok"; data: LocalModelsResult }
  | { status: "error"; message: string };

// What the confirmation is about. Every destructive action becomes one of these
// first — there is no path from a click straight to a delete.
type Pending =
  | { kind: "repo"; repo: LocalModelRepo }
  | { kind: "revision"; repo: LocalModelRepo; revision: LocalModelRevision }
  // Prune carries no selection: the dialog owns the age, and derives the list
  // from the listing on screen, so the two can never disagree.
  | { kind: "prune" };

function shortCommit(commit: string): string {
  // Cache directories are named by full sha; the first 7 are what anyone reads.
  return /^[0-9a-f]{16,}$/i.test(commit) ? commit.slice(0, 7) : commit;
}

function staleRepos(repos: LocalModelRepo[], days: number): LocalModelRepo[] {
  const cutoff = Date.now() / 1000 - days * 86400;
  // A repo with no readable timestamp is left alone rather than swept: "we
  // don't know when this was used" is not evidence that it is cold.
  return repos.filter((r) => r.lastUsed !== null && r.lastUsed < cutoff);
}

// The revisions drawer: fetched per repo when a row is expanded, since
// resolving every snapshot symlink in every repo is exactly what the
// biggest-first overview avoids doing.
function Revisions({
  repo,
  onDelete,
}: {
  repo: LocalModelRepo;
  onDelete: (revision: LocalModelRevision) => void;
}) {
  const [rows, setRows] = useState<LocalModelRevision[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    getLocalModelRevisions(repo.dir).then(
      (r) => alive && setRows(r.revisions),
      (e: Error) => alive && setError(e.message),
    );
    return () => {
      alive = false;
    };
    // Re-fetched when the repo's revision count changes under a deletion.
  }, [repo.dir, repo.revisions]);

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!rows) return <div className="lm-drawer-note">Reading revisions…</div>;
  if (!rows.length) return <div className="lm-drawer-note">No revisions materialised.</div>;
  return (
    <div className="lm-drawer">
      {rows.map((rev) => (
        <div className="lm-rev" key={rev.commit}>
          <span className="lm-rev-main">
            <span className="lm-rev-commit cc-mono">{shortCommit(rev.commit)}</span>
            {rev.refs.map((ref) => (
              <span className="cc-pill" key={ref}>
                {ref}
              </span>
            ))}
            <span className="lm-rev-meta">
              {rev.files} {rev.files === 1 ? "file" : "files"}
              {rev.shared ? ` · ${formatSize(rev.shared)} shared with other revisions` : ""}
            </span>
          </span>
          {/* The size that matters is what deleting THIS revision frees, not
              what it appears to contain — see the endpoint's docstring. */}
          <span className="lm-rev-size" title="Freed by deleting this revision">
            {formatSize(rev.size)}
          </span>
          <button
            type="button"
            className="lm-iconbtn lm-iconbtn-danger"
            title={`Delete revision ${shortCommit(rev.commit)}`}
            aria-label={`Delete revision ${shortCommit(rev.commit)}`}
            onClick={() => onDelete(rev)}
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}

function RepoRow({
  repo,
  expanded,
  onToggle,
  onDeleteRepo,
  onDeleteRevision,
}: {
  repo: LocalModelRepo;
  expanded: boolean;
  onToggle: () => void;
  onDeleteRepo: () => void;
  onDeleteRevision: (revision: LocalModelRevision) => void;
}) {
  const when = timeAgo(repo.lastUsed ?? repo.mtime);
  return (
    <div className={"lm-row" + (expanded ? " lm-row-open" : "")}>
      <div className="lm-row-head">
        {/* The folder opens in the explorer, and stays a real <a href> so
            middle-click and "copy link" behave (same contract as the bookmark
            cards). The row's buttons live OUTSIDE the anchor — a button inside
            a link is neither valid nor operable by keyboard. */}
        <a
          className="lm-row-link"
          href={urlForFsPath(repo.path)}
          title={repo.path}
          onClick={(e) => {
            if (
              e.defaultPrevented ||
              e.button !== 0 ||
              e.metaKey ||
              e.ctrlKey ||
              e.shiftKey ||
              e.altKey
            )
              return;
            e.preventDefault();
            navigate(repo.path, { isDir: true });
          }}
        >
          <span className="lm-row-name">
            {repo.id} <span className="cc-pill">{repo.kind}</span>
          </span>
          <span className="lm-row-meta">
            {repo.files} {repo.files === 1 ? "file" : "files"}
            {repo.revisions > 1 ? ` · ${repo.revisions} revisions` : ""}
            {repo.refs.length ? ` · ${repo.refs.join(", ")}` : ""}
            {when ? ` · used ${when}` : ""}
          </span>
        </a>
        <span
          className="lm-row-size"
          title={repo.mtime ? `Last changed ${formatMtimeFull(repo.mtime)}` : undefined}
        >
          {formatSize(repo.size)}
        </span>
        {/* Only offered where it means something: with a single revision,
            deleting "the revision" and deleting the repo are the same act, and
            two controls for it would just ask the user to tell them apart. */}
        {repo.revisions > 1 && (
          <button
            type="button"
            className={"lm-iconbtn" + (expanded ? " lm-iconbtn-on" : "")}
            title={expanded ? "Hide revisions" : "Show revisions"}
            aria-expanded={expanded}
            onClick={onToggle}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d={expanded ? "m6 15 6-6 6 6" : "m6 9 6 6 6-6"} />
            </svg>
          </button>
        )}
        <button
          type="button"
          className="lm-iconbtn lm-iconbtn-danger"
          title={`Delete ${repo.id}`}
          aria-label={`Delete ${repo.id}`}
          onClick={onDeleteRepo}
        >
          ✕
        </button>
      </div>
      {/* Same predicate as the expander above, so a repo that drops to one
          revision under a deletion collapses itself rather than stranding an
          open drawer with no control left to close it. */}
      {expanded && repo.revisions > 1 && <Revisions repo={repo} onDelete={onDeleteRevision} />}
    </div>
  );
}

// The prune dialog: pick an age, read the list it selects, confirm. The
// selection is client-side over `lastUsed` from the last scan, and what gets
// sent is the resulting NAMES — so what the server deletes is exactly what this
// list showed, never a threshold it re-evaluates against different state.
function PruneModal({
  repos,
  busy,
  onCancel,
  onConfirm,
}: {
  repos: LocalModelRepo[];
  busy: boolean;
  onCancel: () => void;
  onConfirm: (days: number, stale: LocalModelRepo[]) => void;
}) {
  const [days, setDays] = useState(DEFAULT_PRUNE_DAYS);
  const stale = staleRepos(repos, days);
  const freed = stale.reduce((sum, r) => sum + r.size, 0);
  return (
    <Modal
      title="Prune unused models"
      busy={busy}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy || !stale.length}
            onClick={() => onConfirm(days, stale)}
          >
            {busy
              ? "Deleting…"
              : stale.length
                ? `Delete ${stale.length} ${stale.length === 1 ? "repo" : "repos"} · ${formatSize(freed)}`
                : "Nothing to prune"}
          </button>
        </>
      }
    >
      <div className="lm-prune-choices">
        {/* A segmented choice in the app's own button vocabulary: the active
            threshold is the primary button, the rest are secondary. A tinted
            border alone was too quiet for the control that decides what gets
            deleted. */}
        {PRUNE_CHOICES.map((choice) => (
          <button
            key={choice}
            type="button"
            className={"btn " + (choice === days ? "btn-primary" : "btn-secondary")}
            aria-pressed={choice === days}
            disabled={busy}
            onClick={() => setDays(choice)}
          >
            {choice} days
          </button>
        ))}
      </div>
      <p>
        Deletes every cached repo not <em>read</em> in the last {days} days. Re-downloading one is
        the full transfer again.
      </p>
      {stale.length ? (
        <ul className="lm-prune-list">
          {stale.map((r) => (
            <li key={r.dir}>
              <span className="lm-prune-name">{r.id}</span>
              <span className="lm-prune-meta">
                {formatSize(r.size)} · used {timeAgo(r.lastUsed) ?? "unknown"}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="cc-unset">Nothing in this cache is that old.</p>
      )}
      {/* The honest caveat: this reads last-READ time, and some setups never
          record it. Said here rather than in a doc nobody opens mid-delete. */}
      <p className="cc-unset">
        Last-read time comes from the filesystem. Volumes mounted <code>noatime</code> never update
        it, so check the dates above before confirming.
      </p>
    </Modal>
  );
}

export default function LocalModels() {
  const [load, setLoad] = useState<Load>({ status: "loading" });
  // Bumped by Refresh to re-run the scan in place. Scanning is a disk walk over
  // every blob, so it happens on mount and on an explicit Refresh — never on a
  // focus/return tick, which would re-walk tens of thousands of files every
  // time the user alt-tabbed back. A delete answers with the fresh listing
  // itself, so it needs no bump either.
  const [reloadKey, setReloadKey] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  // Per-target refusals from the last delete (a symlinked repo, a row that was
  // already gone). A banner rather than a toast: it names things the user asked
  // for and did not get.
  const [failures, setFailures] = useState<string[]>([]);

  useEffect(() => {
    let alive = true;
    setLoad({ status: "loading" });
    getLocalModels().then(
      (data) => {
        // The page's own answer is authoritative for the sidebar gate: a cache
        // that exists (or has just appeared) shouldn't wait out the probe TTL,
        // and a sidebar already on screen hears this immediately. Published
        // BEFORE the `alive` check, deliberately: the gate is shared state, and
        // the sidebar it is for outlives this page (navigating between shell
        // routes unmounts the page and keeps ShellSidebar mounted). A scan the
        // user navigated away from still learned the truth — dropping it would
        // hide a real cache for the rest of the TTL. Only the local setState,
        // which belongs to a component that may be gone, sits behind the guard.
        publishAvailable(data.exists);
        if (!alive) return;
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

  const runDelete = async (targets: LocalModelDeleteTarget[], label: string) => {
    setBusy(true);
    try {
      const result = await deleteLocalModels(targets);
      publishAvailable(result.exists);
      setLoad({ status: "ok", data: result });
      setFailures(
        result.failures.map((f) => `${f.dir ?? "target"}${f.revision ? ` @ ${shortCommit(f.revision)}` : ""}: ${f.error}`),
      );
      // A deletion that freed nothing is worth saying out loud too — it means
      // every target failed, and the banner beside it says why.
      pushToast({
        msg: result.freed ? `Freed ${formatSize(result.freed)} — ${label}` : `Nothing deleted — ${label}`,
        tone: result.failures.length ? "error" : "info",
      });
      setPending(null);
    } catch (e) {
      // A transport/guard failure never reached the disk, so the listing on
      // screen is still true — surface it and leave the dialog open.
      setFailures([(e as Error).message]);
    } finally {
      setBusy(false);
    }
  };

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
          <div className="lm-head-actions">
            {repos.length > 0 && (
              <button
                type="button"
                className="btn btn-secondary"
                disabled={load.status === "loading"}
                onClick={() => setPending({ kind: "prune" })}
              >
                Prune…
              </button>
            )}
            <button
              type="button"
              className="btn"
              onClick={() => setReloadKey((k) => k + 1)}
              disabled={load.status === "loading"}
            >
              {load.status === "loading" ? "Scanning…" : "Refresh"}
            </button>
          </div>
        </div>
        {load.status === "error" && <ErrorBanner>{load.message}</ErrorBanner>}
        {failures.length > 0 && (
          <ErrorBanner>
            {failures.map((f) => (
              <div key={f}>{f}</div>
            ))}
          </ErrorBanner>
        )}
        {load.status === "loading" && <p className="cc-empty">Reading the Hugging Face cache…</p>}
        {data &&
          (repos.length ? (
            <div className="lm-list">
              {repos.map((r) => (
                <RepoRow
                  key={r.path}
                  repo={r}
                  expanded={expanded === r.dir}
                  onToggle={() => setExpanded(expanded === r.dir ? null : r.dir)}
                  onDeleteRepo={() => setPending({ kind: "repo", repo: r })}
                  onDeleteRevision={(revision) => setPending({ kind: "revision", repo: r, revision })}
                />
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

      {pending?.kind === "prune" && (
        <PruneModal
          repos={repos}
          busy={busy}
          onCancel={() => setPending(null)}
          onConfirm={(days, stale) =>
            runDelete(
              stale.map((r) => ({ dir: r.dir })),
              `pruned ${stale.length} unused ${days} days or more`,
            )
          }
        />
      )}

      {pending?.kind === "repo" && (
        <Modal
          title={`Delete ${pending.repo.id}?`}
          busy={busy}
          onClose={() => setPending(null)}
          footer={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => setPending(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={busy}
                onClick={() => runDelete([{ dir: pending.repo.dir }], `deleted ${pending.repo.id}`)}
              >
                {busy ? "Deleting…" : `Delete · ${formatSize(pending.repo.size)}`}
              </button>
            </>
          }
        >
          <p>
            Removes every revision of <b>{pending.repo.id}</b> from this machine and frees{" "}
            <b>{formatSize(pending.repo.size)}</b>. Anything that needs it again downloads it again.
          </p>
          <p className="cc-mono cc-unset">{pending.repo.path}</p>
        </Modal>
      )}

      {pending?.kind === "revision" && (
        <Modal
          title={`Delete revision ${shortCommit(pending.revision.commit)}?`}
          busy={busy}
          onClose={() => setPending(null)}
          footer={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => setPending(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={busy}
                onClick={() =>
                  runDelete(
                    [{ dir: pending.repo.dir, revision: pending.revision.commit }],
                    `deleted ${pending.repo.id} @ ${shortCommit(pending.revision.commit)}`,
                  )
                }
              >
                {busy ? "Deleting…" : `Delete · ${formatSize(pending.revision.size)}`}
              </button>
            </>
          }
        >
          <p>
            Removes revision <span className="cc-mono">{shortCommit(pending.revision.commit)}</span>{" "}
            of <b>{pending.repo.id}</b>, freeing <b>{formatSize(pending.revision.size)}</b>.
            {pending.revision.shared > 0 && (
              <>
                {" "}
                The <b>{formatSize(pending.revision.shared)}</b> it shares with the other revisions
                stays.
              </>
            )}
          </p>
          {pending.revision.refs.length > 0 && (
            <p>
              {pending.revision.refs.join(", ")}{" "}
              {pending.revision.refs.length === 1 ? "points" : "point"} at this revision and will be
              removed with it.
            </p>
          )}
          {pending.repo.revisions === 1 && (
            <p>It is the only revision left, so the whole repo folder goes.</p>
          )}
        </Modal>
      )}
    </div>
  );
}
