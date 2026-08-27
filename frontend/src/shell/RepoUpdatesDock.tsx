// The repo-updates notification card (SPEC §36): its OWN sibling entry in
// the bottom-right column, one row for every git repo the server has
// noticed is behind its remote's default branch, with an opt-in action to
// fix it.
//
// It used to be rows PINNED INSIDE the jobs/downloads card, exempt from that
// card's fold and invisible to its header and its Clear button. That shape
// broke the jobs card in four ways at once: with zero jobs and zero queue
// but one repo row, `jobsSummary` fell through to "0 finished"; the jobs
// card's collapse toggle did nothing (repo rows were exempt from the fold,
// and there were no job rows left to fold); Clear disappeared (`clearable`
// counted jobs only); and there was no way to dismiss a repo row at all. All
// four were the same root cause — a second, unrelated kind of row wedged
// inside a card whose header, collapse and Clear were never built to know
// about it. This card fixes that by existing on its own: the jobs card goes
// back to being only about jobs and the scheduled queue (DownloadManager.tsx),
// and this one owns its own header, its own collapse, its own Clear, and a
// per-row ✕.
//
// The check that DECIDES a repo belongs here runs server-side, throttled per
// repo root, triggered from GET /render opening an app (fused_render/
// git_upstream.py — see its module docstring for the full reasoning). This
// component only polls the RESULT (GET /api/git-upstream) and renders it;
// it never itself decides which repos are behind, and it never fetches git
// directly.
//
// Same component/lib split as QueueDock.tsx/queue-dock-lib.ts and
// DownloadManager.tsx/jobs.ts: row shaping, the branch-dependent action
// choice, the dismissal rule and the header text are pure functions in
// repo-updates-lib.ts, testable without a DOM; polling, mutation calls and
// pixels live here. `RepoUpdatesCardView` is the pure, props-in half of
// THIS card (mirroring `DownloadManagerView`) — no polling, no network, no
// `window`/`document` — so RepoUpdatesDock.test.tsx can render it directly
// with a fixed row list, the same way DownloadManager.test.tsx renders
// `DownloadManagerView`.
//
// WHY THIS FILE LIVES IN shell/, NOT platform/ — the same reason
// NotificationHost.tsx gives for QueueDock (platform/ui/NotificationHost.tsx
// §"activity"): resolving a repo root to an explorer route is shell
// knowledge, and frontend/scripts/check-boundaries.mjs forbids platform
// importing shell. `navigate` (Fix with Claude's hop) lives in
// platform/lib/router, which shell may import freely — but the
// STAGED-PROMPT store this row writes into is explorer/lib territory, which
// only shell-side code reaches.
import { useCallback, useEffect, useRef, useState } from "react";
import { stageClaudeAsk } from "@apps/explorer/lib/pending-claude-ask";
import { getJson, postJson } from "@platform/lib/api";
import { navigate } from "@platform/lib/router";
import {
  repoActionLabel,
  repoFixPrompt,
  repoRows,
  repoStatusText,
  repoUpdatesSummary,
  visibleRepoRows,
  type RepoAction,
  type RepoRow,
  type RepoStatus,
} from "@shell/repo-updates-lib";

// Same order of magnitude as QueueDock's own poll: fast enough that a row
// appears soon after an app open triggers the server-side check, slow
// enough to be a permanent background poll in every shell. The check itself
// is throttled server-side (git_upstream.CHECK_TTL_S), so polling faster
// than that would only ever re-read the same cached answer.
const POLL_MS = 6000;

// This card's own persisted collapse preference — a DISTINCT key from the
// jobs card's `fused-render:jobs-collapsed` (DownloadManager.tsx). The two
// cards are separate surfaces now (decision A) with separate histories a
// user might want folded independently; sharing one key would make
// collapsing one silently collapse the other on next load.
const COLLAPSED_KEY = "fused-render:repo-updates-collapsed";

function loadCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false; // private mode / disabled storage — expanded is the honest default
  }
}

function saveCollapsed(collapsed: boolean): void {
  try {
    localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    /* best-effort, like every other persisted chrome flag */
  }
}

type MutationResult = { ok: boolean; reason?: string; message?: string };

function useRepoUpdates() {
  const [repos, setRepos] = useState<RepoStatus[]>([]);
  const pollRef = useRef<() => void>(() => {});

  useEffect(() => {
    let disposed = false;
    let timer = 0;
    const poll = async () => {
      // Cancel whatever is still pending from a PRIOR call to this same
      // `poll` — without this, `refresh()` (below) calling `pollRef.current()`
      // out of band, on top of the timer already ticking, forked a second
      // setTimeout chain: every Update/Rebase/Switch click permanently
      // doubled the number of concurrent /api/git-upstream poll loops for
      // the session, and only the LAST-assigned timer id was ever cleared
      // on unmount.
      window.clearTimeout(timer);
      try {
        const data = await getJson<{ repos?: RepoStatus[] }>("/api/git-upstream");
        if (!disposed) setRepos(data.repos || []);
      } catch {
        // Best-effort, like every other poll in this card: a failed read
        // leaves the last snapshot standing rather than clearing the rows.
      }
      if (!disposed) timer = window.setTimeout(poll, POLL_MS);
    };
    pollRef.current = poll;
    poll();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, []);

  const refresh = useCallback(() => pollRef.current(), []);
  return { repos, refresh };
}

// Held at MODULE level, not component state, so a remount (switching panes
// or panels tears this component down and back up) does not forget what the
// user just dismissed — the same reason a page reload is the one case this
// deliberately does NOT survive: there is no server state backing a
// dismissal (decision C), only this in-memory map, and a reload starting
// fresh is an acceptable, documented trade rather than reaching for
// localStorage for something this ephemeral.
let moduleDismissed: Record<string, number> = {};

function useDismissed() {
  const [dismissed, setDismissedState] = useState<Record<string, number>>(moduleDismissed);

  const dismissOne = useCallback((root: string, checkedAt: number) => {
    moduleDismissed = { ...moduleDismissed, [root]: checkedAt };
    setDismissedState(moduleDismissed);
  }, []);

  const dismissAll = useCallback((rows: RepoRow[]) => {
    const next = { ...moduleDismissed };
    for (const row of rows) next[row.repo.root] = row.repo.checked_at;
    moduleDismissed = next;
    setDismissedState(next);
  }, []);

  return { dismissed, dismissOne, dismissAll };
}

function RepoRowView({
  row,
  onDone,
  onDismiss,
}: {
  row: RepoRow;
  onDone: (result: MutationResult) => void;
  onDismiss: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<MutationResult | null>(null);

  const run = async (action: RepoAction) => {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    try {
      const result = await postJson<MutationResult>("/api/git-upstream", {
        action,
        root: row.repo.root,
      });
      if (!result.ok) setFailure(result);
      onDone(result);
    } catch {
      setFailure({ ok: false, message: "check your connection and retry" });
    } finally {
      setBusy(false);
    }
  };

  const fixWithClaude = () => {
    if (!failure) return;
    const prompt = repoFixPrompt(row, failure.message || "unknown error", failure.reason);
    stageClaudeAsk(row.repo.root, prompt);
    navigate(row.repo.root, { isDir: true });
  };

  return (
    <div className="q-row">
      <div className="q-row-head">
        <span className="q-title" title={row.repo.root}>
          {row.name}
        </span>
        {/* Primary, then secondary when present, then the dismiss ✕ — the
            same left-to-right order every row in this card follows. */}
        <button
          type="button"
          className="q-all"
          onClick={() => run(row.primaryAction)}
          disabled={busy}
        >
          {busy ? "Working…" : repoActionLabel(row.primaryAction, row.repo.default_branch)}
        </button>
        {row.secondaryAction && (
          <button
            type="button"
            className="q-all"
            onClick={() => run(row.secondaryAction as RepoAction)}
            disabled={busy}
          >
            {repoActionLabel(row.secondaryAction, row.repo.default_branch)}
          </button>
        )}
        <button
          type="button"
          className="dl-x"
          onClick={onDismiss}
          title="Dismiss"
          aria-label={`Dismiss ${row.name}`}
        >
          ✕
        </button>
      </div>
      <div className="q-status">{failure ? failure.message : repoStatusText(row)}</div>
      {/* Refusal, not error text alone — the same failure-toast rule the git
          companion's own rows follow: a refusal is spoken AND offers a way
          out, never just swallowed. This surface has no chat of its own
          (unlike the git companion), so the way out is navigating to the
          repo and staging the ask for whatever Claude-capable surface mounts
          there (pending-claude-ask.ts) rather than calling
          `window._fusedClaudeAsk` directly. */}
      {failure && (
        <button type="button" className="q-all" onClick={fixWithClaude}>
          Fix with Claude
        </button>
      )}
    </div>
  );
}

/**
 * The card's pure, props-in half — everything DownloadManagerView is for the
 * jobs card, for the same reason: no polling, no network, no
 * `window`/`document`, so RepoUpdatesDock.test.tsx can render it directly.
 *
 * THE FOLD TAKES EVERY ROW, unlike the jobs card's partial fold (which
 * pins the queue's rows and a live-run stand-in outside the collapse — see
 * DownloadManager.tsx's own comment on why). That asymmetry does not apply
 * here: a repo row has no in-flight message it would strand mid-turn by
 * being hidden, the header already names how many updates are waiting
 * (`repoUpdatesSummary`) whether or not the list is open, and every row is
 * individually dismissible — so collapsing this list loses nothing a user
 * cannot immediately recover by expanding it again or by pressing ✕. Folding
 * everything is therefore the simpler, honest choice for a card whose only
 * job is "how many, and do you want to see them right now".
 */
export function RepoUpdatesCardView({
  rows,
  dismissed,
  collapsed,
  onToggle,
  onDismiss,
  onDismissAll,
  onDone,
}: {
  rows: RepoRow[];
  dismissed: Record<string, number>;
  collapsed: boolean;
  onToggle: () => void;
  onDismiss: (root: string, checkedAt: number) => void;
  onDismissAll: (visible: RepoRow[]) => void;
  onDone: (result: MutationResult) => void;
}) {
  const visible = visibleRepoRows(rows, dismissed);
  // Nothing to say — render nothing at all, no chrome, matching the jobs
  // card's own empty-card rule: a picture of what is happening now, so an
  // empty one is not an empty state with a header, it is no card.
  if (visible.length === 0) return null;

  return (
    <div className="dl-host">
      <div className="dl-head">
        <button
          className="dl-toggle"
          onClick={onToggle}
          aria-expanded={!collapsed}
          title={collapsed ? "Show updates" : "Hide updates"}
        >
          <span className={"dl-chevron" + (collapsed ? " is-collapsed" : "")} aria-hidden="true">
            ⌄
          </span>
          <span className="dl-summary">{repoUpdatesSummary(visible)}</span>
        </button>
        <button
          className="dl-clear"
          onClick={() => onDismissAll(visible)}
          title="Dismiss every visible update"
        >
          Clear
        </button>
      </div>
      <div className={"dl-rows" + (collapsed ? " is-folded" : "")}>
        {visible.map((row) => (
          <RepoRowView
            key={row.repo.root}
            row={row}
            onDone={onDone}
            onDismiss={() => onDismiss(row.repo.root, row.repo.checked_at)}
          />
        ))}
      </div>
    </div>
  );
}

export default function RepoUpdatesDock() {
  const { repos, refresh } = useRepoUpdates();
  const rows = repoRows(repos);
  const { dismissed, dismissOne, dismissAll } = useDismissed();
  const [collapsed, setCollapsed] = useState(loadCollapsed);

  const toggle = () => {
    setCollapsed((was) => {
      saveCollapsed(!was);
      return !was;
    });
  };

  return (
    <RepoUpdatesCardView
      rows={rows}
      dismissed={dismissed}
      collapsed={collapsed}
      onToggle={toggle}
      onDismiss={dismissOne}
      onDismissAll={dismissAll}
      onDone={() => refresh()}
    />
  );
}
