// The repo-updates half of the ONE bottom-right activity card (SPEC §36):
// a row for every git repo the server has noticed is behind its remote's
// default branch, with an opt-in action to fix it.
//
// The check that DECIDES a repo belongs here runs server-side, throttled per
// repo root, triggered from GET /render opening an app (fused_render/
// git_upstream.py — see its module docstring for the full reasoning). This
// component only polls the RESULT (GET /api/git-upstream) and renders it;
// it never itself decides which repos are behind, and it never fetches git
// directly.
//
// Same component/lib split as QueueDock.tsx/queue-dock-lib.ts: row shaping
// and the branch-dependent action choice are pure functions in
// repo-updates-lib.ts, testable without a DOM; polling, mutation calls, and
// pixels live here.
//
// WHY THIS FILE LIVES IN shell/, NOT platform/ — the same reason
// NotificationHost.tsx gives for QueueDock (platform/ui/NotificationHost.tsx
// §"activity"): resolving a repo root to an explorer route is shell
// knowledge, and frontend/scripts/check-boundaries.mjs forbids platform
// importing shell. `navigate` (Fix with Claude's hop, wired in a later
// commit) lives in platform/lib/router, which shell may import freely — but
// the STAGED-PROMPT store this row writes into is explorer/lib territory,
// which only shell-side code reaches.
import { useCallback, useEffect, useRef, useState } from "react";
import { stageClaudeAsk } from "@apps/explorer/lib/pending-claude-ask";
import { getJson, postJson } from "@platform/lib/api";
import { navigate } from "@platform/lib/router";
import type { RepoUpdatesSlot } from "@platform/ui/DownloadManager";
import {
  repoActionLabel,
  repoFixPrompt,
  repoRows,
  repoStatusText,
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

type MutationResult = { ok: boolean; reason?: string; message?: string };

function useRepoUpdates() {
  const [repos, setRepos] = useState<RepoStatus[]>([]);
  const pollRef = useRef<() => void>(() => {});

  useEffect(() => {
    let disposed = false;
    let timer = 0;
    const poll = async () => {
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

function RepoRowView({
  row,
  onDone,
}: {
  row: RepoRow;
  onDone: (result: MutationResult) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<MutationResult | null>(null);
  const action: RepoAction = row.primaryAction;

  const run = async () => {
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
        <button type="button" className="q-all" onClick={run} disabled={busy}>
          {busy ? "Working…" : repoActionLabel(action)}
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
 * The slot's rows, plus its own poll-and-refresh — a hook rather than a
 * component so a single component (QueueDock, the ONE place a `<DownloadManager>`
 * is instantiated in this shell) can build the whole card from both halves in
 * one render, exactly as it already does for its own `QueueSlot`. See
 * DownloadManager.tsx's `RepoUpdatesSlot` for why this is a second NAMED slot
 * rather than a generalisation to N.
 */
export function useRepoUpdatesSlot(): RepoUpdatesSlot {
  const { repos, refresh } = useRepoUpdates();
  const rows = repoRows(repos);
  return {
    count: rows.length,
    rows: rows.map((row) => (
      <RepoRowView key={row.repo.root} row={row} onDone={() => refresh()} />
    )),
  };
}

// Standalone entry point, for anything that wants the rows on their own
// (tests, a future surface outside the activity card) without reaching into
// the hook directly. Not used by the production card — QueueDock calls
// `useRepoUpdatesSlot` itself, because the rows have to land inside the SAME
// `<DownloadManager>` its own queue rows do.
export default function RepoUpdatesDock() {
  const slot = useRepoUpdatesSlot();
  return <>{slot.rows}</>;
}
