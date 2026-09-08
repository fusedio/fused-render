// The repo-updates notification card (SPEC §36): its OWN sibling entry in
// the status bar (D563, formerly the bottom-right floating column), one row
// for every git repo the server has noticed is behind its remote's default
// branch, with an opt-in action to fix it.
//
// It used to be rows PINNED INSIDE the jobs/downloads card, exempt from that
// card's fold and invisible to its header and its Clear button. That shape
// broke the jobs card in four ways at once: with zero jobs and zero queue
// but one repo row, `jobsSummary` (since deleted) fell through to "0 finished"; the jobs
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
// Same component/lib split as ActivityDock.tsx/queue-dock-lib.ts and
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
// StatusBar.tsx gives for ActivityDock (platform/ui/StatusBar.tsx
// §"activity"): resolving a repo root to an explorer route is shell
// knowledge, and frontend/scripts/check-boundaries.mjs forbids platform
// importing shell. `navigate` (Fix with Claude's hop) lives in
// platform/lib/router, which shell may import freely — but the
// STAGED-PROMPT store this row writes into is explorer/lib territory, which
// only shell-side code reaches.
import { useCallback, useEffect, useRef, useState } from "react";
import { stageClaudeAsk } from "@apps/explorer/lib/pending-claude-ask";
import { dismissLanPairing, getJson, getLanPairings, postJson } from "@platform/lib/api";
import type { LanPairingEvent } from "@platform/lib/api";
import { navigate, navigateUrl } from "@platform/lib/router";
import { useStatusChip, type StatusChipState } from "@platform/lib/statusChip";
import StatusChip from "@platform/ui/StatusChip";
import NotificationCard from "@platform/ui/NotificationCard";
import type { NotificationCardDismiss } from "@platform/ui/NotificationCard";
// `JobRow` reused verbatim for a terminal job (D586) — shell may import
// platform (frontend/scripts/check-boundaries.mjs); the reverse is what is
// forbidden, which is also why the failures reach this section as a PROP
// from the shell rather than by this file reaching into the jobs poll.
import { JobRow } from "@platform/ui/DownloadManager";
import { clearFinishedJobs, isFailure, jobsAfterClear } from "@platform/lib/jobs";
import type { Job } from "@platform/lib/jobs";
import {
  attentionRows,
  attentionDismissSignature,
  visibleAttentionRows,
  type AttentionRow,
} from "@shell/tasks-lib";
import { useTasksPulseRows } from "@shell/tasksPulse";
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

// Same order of magnitude as ActivityDock's own poll: fast enough that a row
// appears soon after an app open triggers the server-side check, slow
// enough to be a permanent background poll in every shell. The check itself
// is throttled server-side (git_upstream.CHECK_TTL_S), so polling faster
// than that would only ever re-read the same cached answer.
const NOOP = () => {};
/** `JobRow`'s optimistic-patch seam, defaulted for callers that hand in a
 *  fixed `terminal` list (the tests). A real one comes from the shell, which
 *  owns the state these rows are drawn from (D586). */
const NOOP_PATCH = () => {};
// THE VOLUME CAP (Task 3): how many terminal jobs draw before the rest fold
// behind "N older notifications". Nothing is ever deleted by this — the
// full list is still there, one click away — it only bounds how tall the
// panel gets on a machine that has finished a great many jobs.
const TERMINAL_VISIBLE_CAP = 5;
const POLL_MS = 6000;
// NOTHING ABOUT THE FOLD IS PERSISTED (D603, user: "on page reload the models
// popover auto opens for some reason"). There used to be a `COLLAPSED_KEY` here
// plus `loadCollapsed`/`saveCollapsed`; all three are DELETED, not merely
// unread — a key that is written and never read is worse than no key, because
// the next reader assumes it means something.
//
// WHY: a `.dl-panel` floats above the page and is dismissed by an outside
// pointer-down or Escape. That is popover behaviour, and a popover that
// restores itself across reloads covers the page on every navigation. "Open"
// is a statement about this moment, not a preference worth remembering. The
// user's own report was not the auto-open path at all — D587's `neverOpen` was
// intact — it was a stored `"0"` from having clicked Models open earlier,
// faithfully restored on every load since, which is indistinguishable from a
// bug from where they sit. This also makes D582's arbiter trivial instead of
// arbitrary (nothing wants to be open at mount) and finally makes "never auto
// open" hold on EVERY path rather than all but one.
//
// The transient `autoOpen`/`autoClose` overrides are untouched; opening is an
// explicit click within the session. Any key left on a real machine from an
// earlier build is inert and needs no migration — nothing reads it.

type MutationResult = { ok: boolean; reason?: string; message?: string };

function useRepoUpdates() {
  const [repos, setRepos] = useState<RepoStatus[]>([]);
  // LAN pairings ride the same poll (third row kind, after D586's failures):
  // a device pairing is a notification, and this card is the notification
  // surface. Server-side store (lan.py `_recent_pairings`), so a dismissal
  // holds across shells and reloads for as long as the server runs.
  const [pairings, setPairings] = useState<LanPairingEvent[]>([]);
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
        const [data, paired] = await Promise.all([
          getJson<{ repos?: RepoStatus[] }>("/api/git-upstream"),
          // Its failure must not take the repo rows down with it (and vice
          // versa): each source degrades alone.
          getLanPairings().catch(() => null),
        ]);
        // Superseded responses are DROPPED, not painted: a fresher read is
        // already in flight, and letting an older one land after it would
        // flick stale rows back onto the screen (the same reason
        // `useJobs` carries its own epoch).
        if (!disposed && mine === generation) {
          setRepos(data.repos || []);
          if (paired) setPairings(paired.pairings || []);
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
  return { repos, pairings, setPairings, refresh };
}

// A device that just paired over the LAN (lan.py): title is the device's
// UA-derived name, the sentence says what pairing means, the ✕ dismisses the
// EVENT server-side (the device itself stays; revoking lives in Preferences).
function PairingRowView({ event, onGone }: { event: LanPairingEvent; onGone: (id: string) => void }) {
  const dismiss = async () => {
    // Optimistic: the row is news, and news the user swatted must go now.
    onGone(event.id);
    try {
      await dismissLanPairing(event.id);
    } catch {
      /* the next poll restores it if the server never heard */
    }
  };
  return (
    <NotificationCard
      title={`${event.name} paired`}
      onDismiss={{ onClick: dismiss, ariaLabel: `Dismiss ${event.name} paired` }}
      status="It can now open your apps from this Wi-Fi. Manage devices in Preferences → Render local network."
    />
  );
}

// A run that has STOPPED TO ASK SOMETHING (Akshil, 2026-09-03: "when the task
// was blocked I did not see any notifications in there"). The fourth row kind,
// and the only one whose subject is still happening: `tasks-lib.attentionRows`
// decides what it says and where it goes, off the pulse poll the shell already
// runs — no endpoint and no second loop of this card's own.
//
// THE WHOLE ROW IS ALSO A CLICK TARGET, when it has somewhere to go — rather
// than a corner "Open" control on an otherwise inert row. Every other row here
// has something to do BESIDES being read (fix the repo, dismiss the failure),
// so its controls have to be aimed at individually; this row has exactly one
// thing to do besides dismiss, and a row with one action should not make a
// person aim at a 60px target inside a 320px one. `NotificationCard`'s
// `rowClick` draws it as a `role="button"` div rather than a real `<button>`,
// because this row also carries the ✕ below, and a button cannot nest inside
// a button.
//
// THE ✕ DISMISSES THE ROW, NOT THE QUESTION. The task stays exactly as parked
// as it was — this only clears its seat in Notifications, the same way
// dismissing a repo row clears a stale "behind" notice without touching the
// repo. The sidebar's Tasks dot is the surface that still says a run is
// waiting; dismissing here never dims it. `tasks-lib.attentionDismissSignature`
// keys the dismissal on the question's own title, so a run asking something
// NEW earns a fresh row even if its key is unchanged.
function AttentionRowView({
  row,
  onDismiss,
}: {
  row: AttentionRow;
  onDismiss: () => void;
}) {
  const title = `${row.taskId} needs your input`;
  const dismiss: NotificationCardDismiss = {
    onClick: onDismiss,
    ariaLabel: `Dismiss ${row.taskId} needs your input`,
  };
  // Nowhere to go — a task naming no folder at all — is drawn as a plain row
  // rather than dropped: the news is still true, and a button that navigates
  // nowhere is worse than text (`attentionRows` on why `href` can be null).
  if (!row.href) {
    return (
      <NotificationCard
        title={title}
        status={row.title}
        statusOneLine
        statusTooltip={row.title}
        onDismiss={dismiss}
      />
    );
  }
  const href = row.href;
  return (
    <NotificationCard
      title={title}
      status={row.title}
      statusOneLine
      statusTooltip={row.title}
      onDismiss={dismiss}
      // `navigateUrl`, not `navigate`: these hrefs are whole /explorer urls with
      // the `_side=claude` handoff and the session id on the query string, and
      // `navigate` takes an fs path and builds its own. No `isDir` hint, for
      // the same reason the calendar popover's thread button — the identical
      // call on the identical value — gives none: a task's target is a folder
      // OR the file the chat was on, and this row cannot tell which.
      rowClick={{ onClick: () => navigateUrl(href), title: `Open ${row.taskId}` }}
    />
  );
}

// Persisted to localStorage (Task 4) — a repo dismissal used to live only in
// a module-level map, forgotten on reload. Same defensive, best-effort
// pattern as sidebarstate.ts: a private window, a full quota or malformed
// JSON all just behave as "nothing dismissed" rather than throwing. Held at
// MODULE level, not component state, so a remount (switching panes or panels
// tears this component down and back up) does not forget what the user just
// dismissed either.
const DISMISSED_KEY = "fused-render:repo-updates-dismissed";
// A waiting-task dismissal (Task 4's remaining piece) gets its OWN key rather
// than sharing `DISMISSED_KEY`'s map: the two are keyed on different id
// spaces (a repo root vs. a task's session/pending key) and expire against
// different signatures (`repoDismissSignature` vs. `attentionDismissSignature`)
// — folding them into one map would risk a collision the moment either id
// space grows a value that looks like the other's.
const ATTENTION_DISMISSED_KEY = "fused-render:attention-dismissed";

function loadDismissed(key: string): Record<string, string> {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object") return {};
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === "string") out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

function saveDismissed(key: string, next: Record<string, string>): void {
  try {
    localStorage.setItem(key, JSON.stringify(next));
  } catch {
    // storage unavailable — dismissal is best-effort, so a failed write is fine
  }
}

let moduleDismissed: Record<string, string> = loadDismissed(DISMISSED_KEY);

function useDismissed() {
  const [dismissed, setDismissedState] = useState<Record<string, string>>(moduleDismissed);

  // Keyed on `repoDismissSignature`, never on `checked_at` (D584 finding 3):
  // a re-check that changes nothing must not resurrect a dismissed row.
  const dismissOne = useCallback((root: string, signature: string) => {
    moduleDismissed = { ...moduleDismissed, [root]: signature };
    saveDismissed(DISMISSED_KEY, moduleDismissed);
    setDismissedState(moduleDismissed);
  }, []);

  const dismissAll = useCallback((rows: RepoRow[]) => {
    const next = { ...moduleDismissed };
    for (const row of rows) next[row.repo.root] = repoDismissSignature(row.repo);
    moduleDismissed = next;
    saveDismissed(DISMISSED_KEY, next);
    setDismissedState(next);
  }, []);

  return { dismissed, dismissOne, dismissAll };
}

let moduleAttentionDismissed: Record<string, string> = loadDismissed(ATTENTION_DISMISSED_KEY);

function useAttentionDismissed() {
  const [dismissed, setDismissedState] =
    useState<Record<string, string>>(moduleAttentionDismissed);

  const dismissOne = useCallback((key: string, signature: string) => {
    moduleAttentionDismissed = { ...moduleAttentionDismissed, [key]: signature };
    saveDismissed(ATTENTION_DISMISSED_KEY, moduleAttentionDismissed);
    setDismissedState(moduleAttentionDismissed);
  }, []);

  return { dismissed, dismissOne };
}

// MIGRATED ONTO `.dl-row`/`.dl-row-head`/`.dl-title`/`.dl-status` (status-bar
// merge, brief item 4), off the parallel `.q-row`/`.q-row-head`/`.q-title`/
// `.q-status` family this row used to share with ActivityDock's own scheduled-
// message rows. The dismiss ✕ was already `.dl-x`, so this is the last of
// this row's classes to converge. `.q-all` STAYS for the primary action
// (Update/Switch) and for "Fix with Claude" below: `.dl-row-cancel` — the
// nearest `.dl-*` equivalent — is documented in notifications.css as reserved
// for a specific verb family (Unload/Stop/Cancel, "prominent, not alarming"),
// which this row's actions are not, so reusing it would misapply that
// weight. `shell/ActivityDock.tsx`'s own queue rows (pending/live scheduled
// messages) are UNTOUCHED by this migration — the brief names this card's
// repo rows specifically — so `.q-row`/`.q-row-head`/`.q-title`/`.q-status`
// stay live, still drawn by those rows.
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

  // THE ONE ACTION, THEN THE DISMISS ✕ — the same left-to-right order every
  // row in this card follows. The ✕ is not merely next, though:
  // notifications.css pins it to the row's right edge with an auto margin
  // (D609, user: "the x icon should always be at the very right of the
  // card"), so any slack in the head falls between the action and the ✕
  // rather than after it — a statement about what the ✕ is (a dismissal of
  // this ROW, not a third step in the action group it would otherwise read
  // as part of) rather than an accident of order.
  //
  // Refusal, not error text alone, on failure — the same failure-toast rule
  // the git companion's own rows follow: a refusal is spoken AND offers a
  // way out, never just swallowed. This surface has no chat of its own
  // (unlike the git companion), so the way out is navigating to the repo and
  // staging the ask for whatever Claude-capable surface mounts there
  // (pending-claude-ask.ts) rather than calling `window._fusedClaudeAsk`
  // directly — `extraAction` (`.q-all`, below the status line).
  return (
    <NotificationCard
      title={row.name}
      titleTooltip={row.repo.root}
      navAction={{
        label:
          busyAction === row.primaryAction
            ? "Working…"
            : repoActionLabel(row.primaryAction, row.repo.default_branch),
        onClick: () => run(row.primaryAction),
        disabled: busyAction !== null,
      }}
      onDismiss={{ onClick: onDismiss, ariaLabel: `Dismiss ${row.name}` }}
      status={failure ? failure.message : repoStatusText(row)}
      extraAction={failure ? { label: "Fix with Claude", onClick: fixWithClaude } : undefined}
    />
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
 * one "nothing here" signal alongside the outlined circle, and the idle
 * sentence itself ("No notifications", D579) lives in the panel that opens
 * beneath the chip. The chip shows the category name and ONE circle —
 * outlined when this section holds nothing, filled when it holds anything
 * (D588/D590, user: "no count. just a circle outlined or filled"). No count,
 * no percentage, no chevron: D573, D581 and D588/D590 removed those in turn.
 */
export function RepoUpdatesCardView({
  rows,
  dismissed,
  collapsed,
  onToggle,
  pinned = false,
  hostProps,
  onDismiss,
  onDismissAll,
  onDone,
  terminal = [],
  pairings = [],
  attention = [],
  attentionDismissed = {},
  onAttentionDismiss,
  onPairingGone,
  onJobsChanged,
  onTerminalPatch,
}: {
  rows: RepoRow[];
  dismissed: Record<string, string>;
  /** Terminal jobs re-routed here from the Jobs section (D586). */
  terminal?: Job[];
  /** Devices that paired over the LAN — the third row kind. */
  pairings?: LanPairingEvent[];
  /** Tasks parked on a question — the fourth row kind (2026-09-03). */
  attention?: AttentionRow[];
  /** Which waiting-task rows a dismissal still hides — keyed and expired the
   *  way `dismissed` is for repo rows, but on `attentionDismissSignature`. */
  attentionDismissed?: Record<string, string>;
  onAttentionDismiss?: (key: string, signature: string) => void;
  onPairingGone?: (id: string) => void;
  /** A terminal row was acted on — ask the jobs poll to re-read. */
  onJobsChanged?: () => void;
  /** Remove a dismissed failure from the shell's own list, immediately. */
  onTerminalPatch?: (fn: (jobs: Job[]) => Job[]) => void;
  collapsed: boolean;
  onToggle: () => void;
  /** Held open by a click (D673) — styles the chip as engaged. */
  pinned?: boolean;
  /** Hover intent + outside-dismiss wiring for the `.dl-host` wrapper, from
   *  `useStatusChip`. Optional: a test that mounts the view bare needs none. */
  hostProps?: StatusChipState["hostProps"];
  onDismiss: (root: string, signature: string) => void;
  onDismissAll: (visible: RepoRow[]) => void;
  onDone: (result: MutationResult) => void;
}) {
  const visible = visibleRepoRows(rows, dismissed);
  const visibleAttention = visibleAttentionRows(attention, attentionDismissed);
  // ONLY TERMINAL JOBS FOLD (Task 3) — a waiting task, a repo row and a
  // pairing are always shown in full below, never counted toward this cap.
  // `olderShown` is local UI state, not a prop: once the reader opens the
  // fold there is no reason for anything outside this view to know or care.
  const [olderShown, setOlderShown] = useState(false);
  const shownTerminal = olderShown ? terminal : terminal.slice(0, TERMINAL_VISIBLE_CAP);
  const olderTerminalCount = terminal.length - shownTerminal.length;
  // EVERY SOURCE DECIDES EVERY DERIVED NUMBER (D586; pairings joined later).
  // The count on the chip, the idle predicate and the empty state all read
  // this one total, so none of them can disagree about what this section
  // holds — a count that still counted only repo rows was the likeliest bug
  // in this change.
  const total =
    visible.length + terminal.length + pairings.length + visibleAttention.length;
  const idle = total === 0;
  // The failure tint MOVED HERE from the Jobs chip (D586), and D662 broadened
  // `terminal` to hold every finished job — done and cancelled as well as
  // error — none of which need the tint. So this stays scoped to `isFailure`
  // (jobs.ts), a real `error`, rather than to "this section holds anything
  // terminal": a `done` row landing here must not turn the chip red just for
  // existing.
  const hasFailure = terminal.some(isFailure);
  // Wraps the chip AND the panel — dismissOnOutside.ts explains why the whole
  // host, not just the panel, is what counts as "inside".
  // THE CHIP READS (D673): "Notifications" with a count pill whenever anything
  // waits — a repo update, a pairing, a finished/failed job, a task asking a
  // question — and the pill turns red when one of those is a failure OR a task
  // is waiting on the person (Akshil, 2026-09-03: a plain count was "not
  // prominent enough to let user know it needs attention"). Muted at zero. The
  // sidebar's Tasks dot is red for the same state; the two agree rather than
  // one of them staying quiet — this corner is where the reader looks for
  // what wants them, and a neutral pill there read as "nothing urgent".
  const asking = visibleAttention.length > 0;
  const tone = hasFailure || asking ? "failure" : total > 0 ? "on" : "idle";
  const ariaLabel =
    total === 0
      ? "Notifications, none"
      : `Notifications, ${total}${
          asking ? ", including a task waiting on you" : hasFailure ? ", including a failure" : ""
        }`;

  return (
    <div className="dl-host" {...hostProps}>
      <StatusChip
        label="Notifications"
        count={total}
        tone={tone}
        open={!collapsed}
        pinned={pinned}
        title={collapsed ? "Show notifications" : "Hide notifications"}
        ariaLabel={ariaLabel}
        onClick={onToggle}
      />
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
              {/* ONE list, FOUR row kinds now — D586's two, plus pairings, plus
                  2026-09-03's waiting tasks — ordered by how much of each fact
                  is still ahead of the reader: a parked run is waiting on THEM,
                  a repo row is actionable (Update / Switch), a pairing is news
                  to read, and a failure is a record of something that already
                  ended. `JobRow` is reused verbatim rather than a new row shape
                  being invented for this — it already draws the title, the
                  failure sentence and the ✕, and it already carries D572's
                  rejected-request surfacing, which is the behaviour a dismiss
                  here most needs to keep. */}
              <div className="dl-rows">
                {/* A WAITING TASK GOES ABOVE EVERYTHING (2026-09-03). It is the
                    only row in this panel whose subject has not finished
                    happening: a repo is behind, a device paired, a job ended —
                    all facts about the past, which will still be true in ten
                    minutes. A parked run is a person being waited on, and the
                    thing being waited on goes first. */}
                {visibleAttention.map((row) => (
                  <AttentionRowView
                    key={row.key}
                    row={row}
                    onDismiss={() =>
                      (onAttentionDismiss ?? NOOP)(row.key, attentionDismissSignature(row))
                    }
                  />
                ))}
                {/* Then pairings: the newest kind of news, and the only one
                    with nothing to act on beyond reading it. */}
                {pairings.map((event) => (
                  <PairingRowView key={event.id} event={event} onGone={onPairingGone ?? NOOP} />
                ))}
                {visible.map((row) => (
                  <RepoRowView
                    key={row.repo.root}
                    row={row}
                    onDone={onDone}
                    onDismiss={() => onDismiss(row.repo.root, repoDismissSignature(row.repo))}
                  />
                ))}
                {shownTerminal.map((job) => (
                  <JobRow
                    key={job.id}
                    job={job}
                    onChanged={onJobsChanged ?? NOOP}
                    // A REAL patcher (D586): `JobRow`'s dismiss calls
                    // `onPatch(js => js.filter(...))` on success, and the
                    // shell's own `terminal` state is exactly that list — so the
                    // row goes the instant the server confirms, instead of
                    // lingering until the next poll. D572's rejected-request
                    // sentence still shows on failure, because the patch only
                    // runs when the request landed.
                    onPatch={onTerminalPatch ?? NOOP_PATCH}
                  />
                ))}
              </div>
              {/* THE VOLUME CAP (Task 3): only TERMINAL jobs ever collapse —
                  a waiting task, a repo row and a pairing are always drawn in
                  full, uncounted by `TERMINAL_VISIBLE_CAP`, because none of
                  them pile up the way a finished job does (a repo stays
                  behind until fixed, one row; a pairing is dismissed the
                  moment it is read). Nothing is dropped, only folded: the
                  count names exactly how many more `JobRow`s are one click
                  away, and clicking it is the only thing that changes
                  `olderShown` — a fresh terminal job arriving never
                  re-collapses a panel the user already opened wide. */}
              {olderTerminalCount > 0 && (
                <button
                  type="button"
                  className="dl-panel-more"
                  onClick={() => setOlderShown(true)}
                >
                  {olderTerminalCount} older notification{olderTerminalCount === 1 ? "" : "s"}
                </button>
              )}
              {/* A FOOTER, NOT A HEADER (D602, user: "notification UI is messed
                  up"). These bulk actions used to render ABOVE the rows, where
                  a full-width padded band holding one small right-aligned
                  button read as a blank row that had failed to render — the
                  first thing in the panel. It was a header when it also
                  carried a count and a title on its left; D588/D590 removed
                  both and left a header with nothing to head. Under the list,
                  the same button reads as acting on what is above it, which is
                  what it does. */}
              {/* ONE "Clear all", not two: a repo row's dismissal is
                  client-side and expires when the repo moves
                  (`repoDismissSignature`), while a terminal job's is
                  server-side and permanent (`dismissJob`/`clearFinishedJobs`)
                  — two different promises, but the reader does not need two
                  buttons to know that; each mechanism just fires for the
                  row kind it owns, best-effort, behind the one click. */}
              {/* PLURALITY, NOT PRESENCE (D604, user with a screenshot of a
                  one-row panel: "the notification card size is still not
                  done"). Clear is dismiss-ALL, so at exactly one row it is
                  redundant — that row's own ✕ does the identical thing in one
                  click, adjacent to the thing it affects — and the band it
                  needs cost 32px of an 88px card, ~36% of the height, most of
                  it empty to the left of one small button with a hairline
                  making the emptiness look deliberate. The threshold is now
                  the COMBINED count of repo rows and terminal jobs, not
                  either counted alone: one stuck repo plus one failed job is
                  two dismissable rows, and a reader looking at two rows and
                  no bulk action has the same "did this break" reaction a
                  count of two of the SAME kind would give them. */}
              {visible.length + terminal.length > 1 && (
                <div className="dl-head">
                  <button
                    className="dl-clear"
                    onClick={() => {
                      if (visible.length > 0) onDismissAll(visible);
                      if (terminal.length > 0) {
                        clearFinishedJobs()
                          .then(() => onTerminalPatch?.(jobsAfterClear))
                          .catch(() => {});
                      }
                    }}
                    title="Dismiss every notification"
                  >
                    Clear all
                  </button>
                </div>
              )}
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
 * TERMINAL JOBS CANNOT OPEN THIS PANEL: `useStatusChip` has no arrival-driven
 * path at all — `open = pinned || hovered`, full stop — so there is no notion
 * of an "announceable" id for anything to reach here as. They DO count for
 * occupancy, so an emptying repo list no longer closes the panel out from
 * under them (code review 2026-08-28, finding 1).
 */
export function RepoUpdatesDockView({
  rows,
  dismissed,
  terminal = [],
  pairings = [],
  attention = [],
  attentionDismissed,
  onAttentionDismiss,
  onPairingGone,
  onDismiss,
  onDismissAll,
  onDone,
  onJobsChanged,
  onTerminalPatch,
  initialCollapsed,
}: {
  rows: RepoRow[];
  dismissed: Record<string, string>;
  /** TERMINAL JOBS (D586, broadened by D662 to done/error/cancelled — not only
   *  `state: "error"`) re-routed out of the Jobs section, drawn beside the
   *  repo rows. Optional and defaulted so every existing caller and test
   *  keeps working unchanged. */
  terminal?: Job[];
  /** LAN pairings — the panel opens only on hover or click (`useStatusChip`'s
   *  `open = pinned || hovered`); a pairing arriving never opens it by
   *  itself. The chip's own numeral is what announces one. */
  pairings?: LanPairingEvent[];
  /** Tasks parked on a question (2026-09-03). Same as pairings: the sidebar's
   *  red Tasks dot already says a run is waiting, and nothing here throws the
   *  panel open over the chat holding the answer — hover or click still
   *  decide when it shows. */
  attention?: AttentionRow[];
  /** Which waiting-task rows a dismissal still hides. */
  attentionDismissed?: Record<string, string>;
  onAttentionDismiss?: (key: string, signature: string) => void;
  onPairingGone?: (id: string) => void;
  onDismiss: (root: string, signature: string) => void;
  onDismissAll: (visible: RepoRow[]) => void;
  onDone: (result: MutationResult) => void;
  /** A terminal row was cancelled/dismissed — ask the jobs poll to re-read.
   *  Optional: a caller with no jobs of its own has nothing to refresh. */
  onJobsChanged?: () => void;
  /** Remove a dismissed failure from the shell's own list, immediately. */
  onTerminalPatch?: (fn: (jobs: Job[]) => Job[]) => void;
  /** TEST SEAM ONLY — the fold's initial value. Every real caller omits it and
   *  gets `true`: sections ALWAYS start collapsed now (D603), unconditionally,
   *  with no stored preference to consult. KEPT rather than deleted with the
   *  persistence, because it is now the ONLY way to mount a section already
   *  open, and ~20 tests here are about what an OPEN panel contains rather than
   *  about the default. Injectable rather than stubbed through
   *  `globalThis.localStorage` for the reason this file documents at length for
   *  `mock.module`: a process-wide replacement has contaminated unrelated
   *  suites here before. */
  initialCollapsed?: boolean;
}) {
  // Hover previews, click pins, NOTHING auto-opens (D673) — a repo update or a
  // finished job is announced by the chip's count pill, not by a panel
  // appearing over the page. `initialCollapsed` is a test seam.
  const chip = useStatusChip("notifications", !(initialCollapsed ?? true));

  return (
    <RepoUpdatesCardView
      rows={rows}
      dismissed={dismissed}
      terminal={terminal}
      pairings={pairings}
      attention={attention}
      attentionDismissed={attentionDismissed}
      onAttentionDismiss={onAttentionDismiss}
      onPairingGone={onPairingGone}
      collapsed={!chip.open}
      onToggle={chip.toggle}
      pinned={chip.pinned}
      hostProps={chip.hostProps}
      onJobsChanged={onJobsChanged}
      onTerminalPatch={onTerminalPatch}
      onDismiss={onDismiss}
      onDismissAll={onDismissAll}
      onDone={onDone}
    />
  );
}

export default function RepoUpdatesDock({
  terminal = [],
  onTerminalPatch,
}: {
  terminal?: Job[];
  onTerminalPatch?: (fn: (jobs: Job[]) => Job[]) => void;
} = {}) {
  const { repos, pairings, setPairings, refresh } = useRepoUpdates();
  const rows = repoRows(repos);
  const { dismissed, dismissOne, dismissAll } = useDismissed();
  const { dismissed: attentionDismissed, dismissOne: attentionDismissOne } =
    useAttentionDismissed();
  // THE SHELL'S EXISTING TASKS POLL, subscribed to — not a fifth timer in this
  // file. `tasksPulse.ts` is one store with one poll behind it (and none at all
  // while the Tasks page is feeding it), which is the whole reason it exists;
  // taking a row subscription here costs this card nothing but a re-render when
  // the answer changes, and it means the notification and the sidebar's red dot
  // can never disagree — they are reading the same array.
  const attention = attentionRows(useTasksPulseRows());
  const pairingGone = useCallback(
    (id: string) => setPairings((list) => list.filter((p) => p.id !== id)),
    [setPairings],
  );

  return (
    <RepoUpdatesDockView
      rows={rows}
      dismissed={dismissed}
      terminal={terminal}
      pairings={pairings}
      attention={attention}
      attentionDismissed={attentionDismissed}
      onAttentionDismiss={attentionDismissOne}
      onPairingGone={pairingGone}
      onTerminalPatch={onTerminalPatch}
      onDismiss={dismissOne}
      onDismissAll={dismissAll}
      onDone={() => refresh()}
    />
  );
}
