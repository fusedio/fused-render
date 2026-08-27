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
import { useAutoExpandOnNew } from "@platform/lib/autoExpand";
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
      // setTimeout chain: every Update/Switch click permanently
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
  // WHICH action is running, not just whether one is (task 12, code review
  // 2026-08-27, from back when a row could offer two buttons — Update/Switch
  // primary plus a Rebase secondary, since removed as too dangerous to offer,
  // D554 amendment): a single shared `busy: boolean` relabeled the button
  // "Working…" no matter which of the two was actually pressed. Kept as
  // `RepoAction | null` rather than collapsing back to a plain boolean —
  // still correct, and still the cheaper property to reason about, for the
  // one button a row offers today, and it costs nothing to leave general
  // enough to survive this row ever growing a second action again.
  const [busyAction, setBusyAction] = useState<RepoAction | null>(null);
  const [failure, setFailure] = useState<MutationResult | null>(null);

  const run = async (action: RepoAction) => {
    if (busyAction !== null) return;
    setBusyAction(action);
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
      setBusyAction(null);
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
        {/* The one action, then the dismiss ✕ — the same left-to-right
            order every row in this card follows. */}
        <button
          type="button"
          className="q-all"
          onClick={() => run(row.primaryAction)}
          disabled={busyAction !== null}
        >
          {busyAction === row.primaryAction
            ? "Working…"
            : repoActionLabel(row.primaryAction, row.repo.default_branch)}
        </button>
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
 * THE FOLD TAKES EVERY ROW — no exemption, no partial fold. That was always
 * this card's own rule (D556), and the jobs card has since adopted the exact
 * same one (D561, user call 2026-08-27: "everything is foldable, even for
 * the job cards" — reversing the exemptions D557/D558 had built there). The
 * two cards now behave identically: collapsed renders NO rows at all — the
 * `.dl-rows` wrapper itself is omitted rather than left as an empty box —
 * and reachability while collapsed lives in the header (this card's rows are
 * each individually dismissible via their own ✕, and the header keeps
 * naming how many updates are waiting via `repoUpdatesSummary`). Whenever
 * the toggle is on screen at all (the card returned non-null, meaning
 * `visible.length > 0`), pressing it always visibly hides or shows every
 * row.
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
      {/* Collapsed shows NO rows and no empty box — see this component's own
          doc comment on why a CSS-only fold (a max-height cap) is not
          enough for a list this short. */}
      {!collapsed && (
        <div className="dl-rows">
          {visible.map((row) => (
            <RepoRowView
              key={row.repo.root}
              row={row}
              onDone={onDone}
              onDismiss={() => onDismiss(row.repo.root, row.repo.checked_at)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * The stateful half that owns collapse (including auto-expand) and wraps
 * the pure `RepoUpdatesCardView` — `rows`/`dismissed`/the callbacks come in
 * as props, no polling, no network, so this can be rendered directly with a
 * fixed row list (RepoUpdatesDock.test.tsx), the same split
 * `DownloadManagerView` uses in its own file for the identical reason.
 *
 * Auto-expand (D561 follow-up, "un collapse when a new one comes") keys off
 * `row.repo.root` per row — a repo not seen before, while the card is
 * collapsed, opens it exactly like the user's own toggle would
 * (`lib/autoExpand.ts` `useAutoExpandOnNew`, shared with DownloadManager.tsx
 * since both cards need the identical wiring around the same pure decision,
 * `jobs.ts` `trackSeenIds`). Fed `visible` — the same post-dismissal list
 * `RepoUpdatesCardView` itself renders — so a row a user just dismissed
 * falls out of the seen set with it: dismissing IS a disappearance, and a
 * repo that goes behind again later (a fresh `checked_at` past the
 * dismissal) is a genuine re-arrival, not a re-trigger of an old one.
 */
export function RepoUpdatesDockView({
  rows,
  dismissed,
  onDismiss,
  onDismissAll,
  onDone,
}: {
  rows: RepoRow[];
  dismissed: Record<string, number>;
  onDismiss: (root: string, checkedAt: number) => void;
  onDismissAll: (visible: RepoRow[]) => void;
  onDone: (result: MutationResult) => void;
}) {
  const [collapsed, setCollapsed] = useState(loadCollapsed);

  const toggle = () => {
    setCollapsed((was) => {
      saveCollapsed(!was);
      return !was;
    });
  };

  const visible = visibleRepoRows(rows, dismissed);
  useAutoExpandOnNew(
    visible.map((row) => row.repo.root),
    collapsed,
    setCollapsed,
    saveCollapsed,
  );

  return (
    <RepoUpdatesCardView
      rows={rows}
      dismissed={dismissed}
      collapsed={collapsed}
      onToggle={toggle}
      onDismiss={onDismiss}
      onDismissAll={onDismissAll}
      onDone={onDone}
    />
  );
}

export default function RepoUpdatesDock() {
  const { repos, refresh } = useRepoUpdates();
  const rows = repoRows(repos);
  const { dismissed, dismissOne, dismissAll } = useDismissed();

  return (
    <RepoUpdatesDockView
      rows={rows}
      dismissed={dismissed}
      onDismiss={dismissOne}
      onDismissAll={dismissAll}
      onDone={() => refresh()}
    />
  );
}
