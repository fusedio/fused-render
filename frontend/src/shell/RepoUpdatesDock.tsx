// The repo-updates notification card (SPEC §36): its OWN sibling entry in
// the status bar (D563, formerly the bottom-right floating column), one row
// for every git repo the server has noticed is behind its remote's default
// branch, with an opt-in action to fix it.
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
// StatusBar.tsx gives for QueueDock (platform/ui/StatusBar.tsx
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
import { useExclusiveSection } from "@platform/lib/exclusiveSection";
// `JobRow` reused verbatim for a failed job (D586) — shell may import
// platform (frontend/scripts/check-boundaries.mjs); the reverse is what is
// forbidden, which is also why the failures reach this section as a PROP
// from the shell rather than by this file reaching into the jobs poll.
import { JobRow } from "@platform/ui/DownloadManager";
import type { Job } from "@platform/lib/jobs";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";
import {
  repoActionLabel,
  repoDismissSignature,
  repoFixPrompt,
  repoRows,
  repoStatusText,
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
const NOOP = () => {};
/** `JobRow`'s optimistic-patch seam, defaulted for callers that hand in a
 *  fixed `failed` list (the tests). A real one comes from the shell, which
 *  owns the state these rows are drawn from (D586). */
const NOOP_PATCH = () => {};
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
  // Has /api/git-upstream answered ONCE? `repos` starts `[]` and stays `[]`
  // for a repo-free machine, so the list alone cannot tell "not asked yet"
  // from "genuinely nothing" — and `useAutoExpandOnNew` needs exactly that
  // distinction to avoid calling every pre-existing repo an arrival on load
  // (D574 bug 2, autoExpand.ts's `ready`).
  const [settled, setSettled] = useState(false);
  const pollRef = useRef<() => void>(() => {});

  useEffect(() => {
    let disposed = false;
    let timer = 0;
    // WHICH poll invocation is the current one. `clearTimeout` alone was not
    // enough (finding 7, code review 2026-08-27): it cancels a PENDING timer,
    // but the fork happens across the `await`. `refresh()` calling
    // `pollRef.current()` while an earlier `poll` was still awaiting left both
    // in flight; each then assigned `timer` on its way out, the second
    // overwriting the first, so one chain was leaked — unclearable on unmount
    // and ticking for the rest of the session, which is exactly the doubling
    // the comment here used to claim was already fixed. A generation counter
    // closes it properly: only the newest invocation may schedule.
    let generation = 0;
    const poll = async () => {
      const mine = ++generation;
      window.clearTimeout(timer);
      try {
        const data = await getJson<{ repos?: RepoStatus[] }>("/api/git-upstream");
        // Superseded responses are DROPPED, not painted: a fresher read is
        // already in flight, and letting an older one land after it would
        // flick stale rows back onto the screen (the same reason
        // `useJobs` carries its own epoch).
        if (!disposed && mine === generation) {
          setRepos(data.repos || []);
          setSettled(true);
        }
      } catch {
        // Best-effort, like every other poll in this card: a failed read
        // leaves the last snapshot standing rather than clearing the rows.
      }
      // Exactly ONE chain survives — whichever invocation is newest.
      if (!disposed && mine === generation) timer = window.setTimeout(poll, POLL_MS);
    };
    pollRef.current = poll;
    poll();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, []);

  const refresh = useCallback(() => pollRef.current(), []);
  return { repos, settled, refresh };
}

// Held at MODULE level, not component state, so a remount (switching panes
// or panels tears this component down and back up) does not forget what the
// user just dismissed — the same reason a page reload is the one case this
// deliberately does NOT survive: there is no server state backing a
// dismissal (decision C), only this in-memory map, and a reload starting
// fresh is an acceptable, documented trade rather than reaching for
// localStorage for something this ephemeral.
let moduleDismissed: Record<string, string> = {};

function useDismissed() {
  const [dismissed, setDismissedState] = useState<Record<string, string>>(moduleDismissed);

  // Keyed on `repoDismissSignature`, never on `checked_at` (D584 finding 3):
  // a re-check that changes nothing must not resurrect a dismissed row.
  const dismissOne = useCallback((root: string, signature: string) => {
    moduleDismissed = { ...moduleDismissed, [root]: signature };
    setDismissedState(moduleDismissed);
  }, []);

  const dismissAll = useCallback((rows: RepoRow[]) => {
    const next = { ...moduleDismissed };
    for (const row of rows) next[row.repo.root] = repoDismissSignature(row.repo);
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
  // D555 amendment): a single shared `busy: boolean` relabeled the button
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
 * this card's own rule (D557), and the jobs card has since adopted the exact
 * same one (D562, user call 2026-08-27: "everything is foldable, even for
 * the job cards" — reversing the exemptions D558/D559 had built there). The
 * two cards now behave identically: collapsed renders a CHIP and nothing
 * else (D563, status bar redesign) — no rows, no Clear, no per-row ✕ — and
 * expanding it is what opens the panel those live in, floating above the
 * status bar rather than pinned inside a header that survives the fold.
 *
 * ALWAYS PRESENT NOW (D565): a `visible.length === 0` card no longer returns
 * null — the bar's three sections stay on screen at all times, this one
 * included. D573 moved WHERE that shows: the chip is a real, always-
 * clickable button either way now (VS Code/Cursor status-bar idiom — hover
 * is the affordance, not a disclosure chevron), muted text (`.is-idle`) is
 * the one remaining "nothing here" signal, and the idle sentence itself
 * ("No notifications", D579) lives in the panel that opens beneath it
 * the chip, which only ever shows the category name and a count now.
 */
export function RepoUpdatesCardView({
  rows,
  dismissed,
  collapsed,
  onToggle,
  onClose,
  onDismiss,
  onDismissAll,
  onDone,
  failed = [],
  onJobsChanged,
  onFailedPatch,
}: {
  rows: RepoRow[];
  dismissed: Record<string, string>;
  /** Failed jobs re-routed here from the Jobs section (D586). */
  failed?: Job[];
  /** A failed row was acted on — ask the jobs poll to re-read. */
  onJobsChanged?: () => void;
  /** Remove a dismissed failure from the shell's own list, immediately. */
  onFailedPatch?: (fn: (jobs: Job[]) => Job[]) => void;
  collapsed: boolean;
  onToggle: () => void;
  /** Background the panel — an outside pointer-down or Escape (D574).
   *  Optional: a caller that mounts this view directly need not dismiss. */
  onClose?: () => void;
  onDismiss: (root: string, signature: string) => void;
  onDismissAll: (visible: RepoRow[]) => void;
  onDone: (result: MutationResult) => void;
}) {
  const visible = visibleRepoRows(rows, dismissed);
  // BOTH SOURCES DECIDE EVERY DERIVED NUMBER (D586). The count on the chip,
  // the idle predicate and the empty state all read this one total, so none of
  // them can disagree about what this section holds — a count that still
  // counted only repo rows was the likeliest bug in this change.
  const total = visible.length + failed.length;
  const idle = total === 0;
  // The failure tint MOVED HERE from the Jobs chip (D586): this is the section
  // that holds failures now, so it is the one worth colouring. Unconditional on
  // there being a failure at all, rather than Jobs' old "everything terminal
  // and something failed" — an `error` row in here is always terminal, so the
  // extra clause had nothing left to say.
  const hasFailure = failed.length > 0;
  // Wraps the chip AND the panel — dismissOnOutside.ts explains why the whole
  // host, not just the panel, is what counts as "inside".
  const hostRef = useRef<HTMLDivElement | null>(null);
  useDismissOnOutside(hostRef, !collapsed, onClose ?? NOOP);

  return (
    <div className="dl-host" ref={hostRef}>
      {/* ALWAYS a real, clickable button now (D573, user: "the chevron
          doesn't belong to the status bar. lets follow vscode/cursor for
          inspiration" — the bar shows the category NAME plus a count, and
          the idle sentence moves into the panel below; see
          `DownloadManagerView`'s own header comment for the fuller
          reasoning, identical here). `repoUpdatesSummary`, which used to
          render the richer "N updates available" phrasing here, is DELETED —
          nothing rendered it after D573 and its docstring had gone stale
          claiming this card does not draw with zero rows (finding 8, code
          review 2026-08-27). */}
      <button
        className={
          "dl-toggle" + (idle ? " is-idle" : "") + (hasFailure ? " is-failure" : "")
        }
        onClick={onToggle}
        aria-expanded={!collapsed}
        title={collapsed ? "Show notifications" : "Hide notifications"}
      >
        {/* `Notifications`, NOT `Updates` (D579, user: "git updates does not
            make sense out of an app. it belongs to 'notifications'") — a repo
            being behind upstream is ONE KIND of notification, not a top-level
            category beside Models and Activity, and `Updates` also collided
            with `Activity` (both read as "stuff that changed", neither says
            whose). The count is unchanged. Nothing else was renamed: this
            file, its `.dl-*`/`.q-*` classes and `repoRows`/`visibleRepoRows`
            all still say "repo updates", which is exactly what they hold —
            `Notifications` is the extensible CATEGORY, so an alert that is
            not a repo update gets a home here without a fourth section. */}
        {/* THE LABEL, and ONE CIRCLE (D588) — see the jobs chip's own comment.
            Filled when this section holds anything: a repo update, a failed
            job, or both. It is also the QUIET SIGNAL for an error-sourced
            notification (D586): a background failure fills this circle and
            opens no panel, since failures never feed the auto-open hook. That
            used to be `.dl-new-dot`'s job; the circle absorbs it, which is
            what let a second filled mark meaning "new" be deleted. */}
        <span className="dl-summary">Notifications</span>
        <span className={"dl-dot" + (total > 0 ? " is-on" : "")} aria-hidden="true" />
      </button>
      {/* The panel — floats ABOVE the status bar, anchored to this chip, and
          exists only while expanded. Collapsed shows no panel at all — see
          this component's own doc comment on why the fold takes every row,
          no exemption, including Clear now that it lives here rather than
          in a header that used to survive the fold. An idle section now
          opens a panel too (D573) — the idle sentence ("No notifications") lives
          there instead of in the chip, which no longer has room for it. */}
      {!collapsed && (
        <div className="dl-panel">
          {idle ? (
            <div className="dl-panel-empty">No notifications</div>
          ) : (
            <>
              {/* Clear takes only the REPO rows — the ones this card's own
                  client-side dismissal model covers. A failed job's dismissal
                  is server-side and permanent (`dismissJob`), so sweeping both
                  under one button would hide two different promises behind it;
                  a failure is dismissed by its own row's ✕ (D586). Omitted
                  entirely when there is no repo row to clear, rather than
                  offering a button that would do nothing. */}
              {visible.length > 0 && (
                <div className="dl-head">
                  <button
                    className="dl-clear"
                    onClick={() => onDismissAll(visible)}
                    title="Dismiss every visible update"
                  >
                    Clear
                  </button>
                </div>
              )}
              {/* ONE list, TWO row kinds (D586). Repo updates first, then
                  failures: the repo rows are the actionable ones (Update /
                  Switch), and a failure is a record of something that already
                  ended. `JobRow` is reused verbatim rather than a new row shape
                  being invented for this — it already draws the title, the
                  failure sentence and the ✕, and it already carries D572's
                  rejected-request surfacing, which is the behaviour a dismiss
                  here most needs to keep. */}
              <div className="dl-rows">
                {visible.map((row) => (
                  <RepoRowView
                    key={row.repo.root}
                    row={row}
                    onDone={onDone}
                    onDismiss={() => onDismiss(row.repo.root, repoDismissSignature(row.repo))}
                  />
                ))}
                {failed.map((job) => (
                  <JobRow
                    key={job.id}
                    job={job}
                    onChanged={onJobsChanged ?? NOOP}
                    // A REAL patcher (D586): `JobRow`'s dismiss calls
                    // `onPatch(js => js.filter(...))` on success, and the
                    // shell's own `failed` state is exactly that list — so the
                    // row goes the instant the server confirms, instead of
                    // lingering until the next poll. D572's rejected-request
                    // sentence still shows on failure, because the patch only
                    // runs when the request landed.
                    onPatch={onFailedPatch ?? NOOP_PATCH}
                  />
                ))}
              </div>
            </>
          )}
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
 * `useAutoExpandOnNew` keys off `row.repo.root` per row and DOES open the
 * panel for a repo not seen before while the card was collapsed (D574
 * reversed the D567 finding that had stopped it; there is no dot any more —
 * D588 replaced every newness mark with the chip's single circle). Shared
 * with DownloadManager.tsx, since both sections need the identical wiring
 * around the same pure decision (`jobs.ts` `trackSeenIds`).
 *
 * Fed `visible` — the same post-dismissal list `RepoUpdatesCardView` itself
 * renders — so a row a user just dismissed falls out of the seen set with it:
 * dismissing IS a disappearance, and a repo that comes back later is a
 * genuine re-arrival rather than a re-trigger of an old one. What makes it
 * "come back" is its POSITION changing (`repoDismissSignature`, D585 finding
 * 3), never a refreshed `checked_at` — a throttled re-check that moved
 * nothing used to resurrect the row every five minutes.
 *
 * FAILED JOBS ARE NOT IN THAT LIST (D586/D588): they reach this section as a
 * prop and fill the circle, but they are deliberately absent from the ids
 * above, which is what makes "a background failure never throws a panel over
 * the page" structural rather than a flag.
 */
export function RepoUpdatesDockView({
  rows,
  dismissed,
  ready,
  failed = [],
  onDismiss,
  onDismissAll,
  onDone,
  onJobsChanged,
  onFailedPatch,
}: {
  rows: RepoRow[];
  dismissed: Record<string, string>;
  /** Has the upstream read answered once (autoExpand.ts's `ready`)? Optional
   *  so a caller that mounts this with a fixed list keeps the old behaviour. */
  ready?: boolean;
  /** FAILED JOBS (D586) — `state: "error"` rows re-routed out of the Jobs
   *  section, drawn beside the repo rows. Optional and defaulted so every
   *  existing caller and test keeps working unchanged. */
  failed?: Job[];
  onDismiss: (root: string, signature: string) => void;
  onDismissAll: (visible: RepoRow[]) => void;
  onDone: (result: MutationResult) => void;
  /** A failed row was cancelled/dismissed — ask the jobs poll to re-read.
   *  Optional: a caller with no jobs of its own has nothing to refresh. */
  onJobsChanged?: () => void;
  /** Remove a dismissed failure from the shell's own list, immediately. */
  onFailedPatch?: (fn: (jobs: Job[]) => Job[]) => void;
}) {
  const [collapsed, setCollapsed] = useState(loadCollapsed);

  const visible = visibleRepoRows(rows, dismissed);
  // A dot for a repo that fell behind while this chip was collapsed, AND
  // (D574) a transient auto-open of this section's own panel. `autoOpen` is
  // never persisted — autoExpand.ts's header on why that write, not the
  // opening itself, was D567's real defect.
  const { autoOpen, autoClose, acknowledge, forceClose } = useAutoExpandOnNew(
    visible.map((row) => row.repo.root),
    collapsed,
    ready,
  );
  // NO SECOND HOOK for the failures any more (D588). It existed only to set
  // `.dl-new-dot` for an error-sourced notification while never opening the
  // panel (D586); with newness marks gone, the one circle below carries that
  // signal directly off `failed.length`, and the "never opens" half is now
  // STRUCTURAL rather than configured — failures are simply not among the ids
  // the auto-open hook above is given, so there is no path from a failure to
  // `setOverride("open")` at all.
  // The saved preference, overridden in EITHER direction by whichever
  // transient flag is standing (D580 adds the closing half; the two are
  // mutually exclusive by construction — autoExpand.ts holds one `Override`,
  // not two independent booleans). `autoClose` is tested first because a
  // drained list beats a stale auto-open that the same drain is retiring.
  const open = autoClose ? false : !collapsed || autoOpen;

  // ONE panel at a time across the whole bar (D582). Only ever CLOSES this
  // section, and only transiently — see `exclusiveSection.ts` on why the
  // arbiter must not touch the saved preference.
  useExclusiveSection("notifications", open, forceClose);

  // ONE unified toggle for a chip whose visible state may be the SAVED
  // preference or either transient override (D580). It acts on what the user
  // SEES — `wantOpen = !open` — then writes the preference only if the
  // preference is what disagrees. That is what keeps D574's rule intact
  // without a special case for it: dismissing an auto-OPENED panel (or
  // reopening an auto-CLOSED one) finds the saved flag already agreeing with
  // the outcome, so clearing the override is the whole of the work and
  // nothing is persisted. A click on a chip whose state came from the
  // preference itself still flips and saves it, exactly as before.
  const toggle = () => {
    const wantOpen = !open;
    acknowledge();
    if (collapsed === wantOpen) {
      saveCollapsed(!wantOpen);
      setCollapsed(!wantOpen);
    }
  };

  // TRANSIENT ONLY — no write to the saved preference (D584 review finding 2).
  // `useDismissOnOutside` fires on any pointer-down outside THIS host, and a
  // click on a SIBLING CHIP is outside it, so the persisting version turned
  // "the user opened Models" into `jobs-collapsed = "1"` plus
  // `repo-updates-collapsed = "1"`. All three keys converged on "1" and the
  // preference became write-only — the exact "the app decided, not the user"
  // failure the D567 guard exists to prevent, arriving through the dismiss
  // path instead of through `forceClose`. So this now IS `forceClose`: the
  // panel goes away, and what the user last chose is left alone.
  const close = forceClose;

  return (
    <RepoUpdatesCardView
      rows={rows}
      dismissed={dismissed}
      failed={failed}
      collapsed={!open}
      onToggle={toggle}
      onClose={close}
      onJobsChanged={onJobsChanged}
      onFailedPatch={onFailedPatch}
      onDismiss={onDismiss}
      onDismissAll={onDismissAll}
      onDone={onDone}
    />
  );
}

export default function RepoUpdatesDock({
  failed = [],
  onFailedPatch,
}: {
  failed?: Job[];
  onFailedPatch?: (fn: (jobs: Job[]) => Job[]) => void;
} = {}) {
  const { repos, settled, refresh } = useRepoUpdates();
  const rows = repoRows(repos);
  const { dismissed, dismissOne, dismissAll } = useDismissed();

  return (
    <RepoUpdatesDockView
      rows={rows}
      dismissed={dismissed}
      ready={settled}
      failed={failed}
      onFailedPatch={onFailedPatch}
      onDismiss={dismissOne}
      onDismissAll={dismissAll}
      onDone={() => refresh()}
    />
  );
}
