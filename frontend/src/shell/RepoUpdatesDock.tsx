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
import { navigate } from "@platform/lib/router";
import { useAutoExpandOnNew } from "@platform/lib/autoExpand";
import { useExclusiveSection } from "@platform/lib/exclusiveSection";
// `JobRow` reused verbatim for a failed job (D586) — shell may import
// platform (frontend/scripts/check-boundaries.mjs); the reverse is what is
// forbidden, which is also why the failures reach this section as a PROP
// from the shell rather than by this file reaching into the jobs poll.
import { JobRow } from "@platform/ui/DownloadManager";
import type { Job } from "@platform/lib/jobs";
import { Button } from "@platform/shadcn/ui/button";
import { DockEmpty, DockFooter, DockRows, StatusBarSection } from "@platform/ui/statusbar/StatusBarSection";
import { DockAction, DockDismiss, DockLine, DockRow, DockRowHead, DockTitle } from "@platform/ui/statusbar/DockRow";
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
 *  fixed `failed` list (the tests). A real one comes from the shell, which
 *  owns the state these rows are drawn from (D586). */
const NOOP_PATCH = () => {};
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
  return { repos, pairings, setPairings, settled, refresh };
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
    <DockRow>
      <DockRowHead>
        <DockTitle>{event.name} paired</DockTitle>
        <DockDismiss onClick={dismiss} title="Dismiss" aria-label={`Dismiss ${event.name} paired`} />
      </DockRowHead>
      <DockLine>It can now open your apps from this Wi-Fi. Manage devices in Preferences → Render local network.</DockLine>
    </DockRow>
  );
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

// The same DockRow shape every panel row wears: title, one action, the ✕
// pinned to the row's right edge (D609), a status sentence under it.
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
    <DockRow>
      <DockRowHead>
        <DockTitle title={row.repo.root}>{row.name}</DockTitle>
        <DockAction onClick={() => run(row.primaryAction)} disabled={busyAction !== null}>
          {busyAction === row.primaryAction
            ? "Working…"
            : repoActionLabel(row.primaryAction, row.repo.default_branch)}
        </DockAction>
        <DockDismiss onClick={onDismiss} title="Dismiss" aria-label={`Dismiss ${row.name}`} />
      </DockRowHead>
      <DockLine>{failure ? failure.message : repoStatusText(row)}</DockLine>
      {/* Refusal, not error text alone: a refusal is spoken AND offers a way
          out. This surface has no chat of its own, so the way out is navigating
          to the repo and staging the ask (pending-claude-ask.ts). */}
      {failure && (
        <Button variant="outline" size="xs" className="mt-1.5" onClick={fixWithClaude}>
          Fix with Claude
        </Button>
      )}
    </DockRow>
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
  onClose,
  onDismiss,
  onDismissAll,
  onDone,
  failed = [],
  pairings = [],
  onPairingGone,
  onJobsChanged,
  onFailedPatch,
}: {
  rows: RepoRow[];
  dismissed: Record<string, string>;
  /** Failed jobs re-routed here from the Jobs section (D586). */
  failed?: Job[];
  /** Devices that paired over the LAN — the third row kind. */
  pairings?: LanPairingEvent[];
  onPairingGone?: (id: string) => void;
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
  // EVERY SOURCE DECIDES EVERY DERIVED NUMBER (D586; pairings joined later).
  // The count on the chip, the idle predicate and the empty state all read
  // this one total, so none of them can disagree about what this section
  // holds — a count that still counted only repo rows was the likeliest bug
  // in this change.
  const total = visible.length + failed.length + pairings.length;
  const idle = total === 0;
  // The failure tint MOVED HERE from the Jobs chip (D586): this is the section
  // that holds failures now, so it is the one worth colouring. Unconditional on
  // there being a failure at all, rather than Jobs' old "everything terminal
  // and something failed" — an `error` row in here is always terminal, so the
  // extra clause had nothing left to say.
  const hasFailure = failed.length > 0;

  return (
    <StatusBarSection
      // `Notifications`, not `Updates` (D579): a repo behind upstream is ONE
      // KIND of notification; the category is extensible (failures, pairings).
      label="Notifications"
      // Filled when this section holds anything (D590) — also the QUIET SIGNAL
      // for a failure (D586): it fills the circle and opens no panel.
      on={total > 0}
      dotLabel={total > 0 ? "notifications waiting" : "no notifications"}
      idle={idle}
      failure={hasFailure}
      open={!collapsed}
      hasRows={!idle}
      title={collapsed ? "Show notifications" : "Hide notifications"}
      onToggle={onToggle}
      onDismiss={onClose ?? NOOP}
    >
      {idle ? (
        <DockEmpty>No notifications</DockEmpty>
      ) : (
        <>
          {/* ONE list, three row kinds: pairings (news to read), repo updates
              (actionable), then failures — `JobRow` reused verbatim, since it
              already carries D572's rejected-request surfacing. */}
          <DockRows>
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
            {failed.map((job) => (
              <JobRow
                key={job.id}
                job={job}
                onChanged={onJobsChanged ?? NOOP}
                // A REAL patcher (D586): the shell's own `failed` state is the
                // list JobRow's dismiss filters, so the row goes the instant
                // the server confirms.
                onPatch={onFailedPatch ?? NOOP_PATCH}
              />
            ))}
          </DockRows>
          {/* A footer, not a header (D602). Clear takes only the REPO rows —
              a failed job's dismissal is server-side and permanent — and is
              plurality-gated (D604): at one row the row's own ✕ is the same
              action with a better name. */}
          {visible.length > 1 && (
            <DockFooter>
              <Button
                variant="outline"
                size="xs"
                onClick={() => onDismissAll(visible)}
                title="Dismiss every visible update"
              >
                Clear
              </Button>
            </DockFooter>
          )}
        </>
      )}
    </StatusBarSection>
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
 * FAILED JOBS CANNOT OPEN THIS PANEL (D586/D588): they reach this section as a
 * prop and fill the circle, but they go to the hook as `alsoDrawn`, never as
 * announceable ids — which is what makes "a background failure never throws a
 * panel over the page" structural rather than a flag. They DO count for
 * occupancy, so an emptying repo list no longer closes the panel out from under
 * them (code review 2026-08-28, finding 1).
 */
export function RepoUpdatesDockView({
  rows,
  dismissed,
  ready,
  failed = [],
  pairings = [],
  onPairingGone,
  onDismiss,
  onDismissAll,
  onDone,
  onJobsChanged,
  onFailedPatch,
  initialCollapsed,
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
  /** LAN pairings — announceable rows: a pairing while the chip is collapsed
   *  auto-opens the panel, which is the whole point of announcing one. */
  pairings?: LanPairingEvent[];
  onPairingGone?: (id: string) => void;
  onDismiss: (root: string, signature: string) => void;
  onDismissAll: (visible: RepoRow[]) => void;
  onDone: (result: MutationResult) => void;
  /** A failed row was cancelled/dismissed — ask the jobs poll to re-read.
   *  Optional: a caller with no jobs of its own has nothing to refresh. */
  onJobsChanged?: () => void;
  /** Remove a dismissed failure from the shell's own list, immediately. */
  onFailedPatch?: (fn: (jobs: Job[]) => Job[]) => void;
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
  const [collapsed, setCollapsed] = useState(initialCollapsed ?? true);

  const visible = visibleRepoRows(rows, dismissed);
  // A transient auto-open of this section's own panel for a repo that fell
  // behind while the chip was collapsed (D574). `autoOpen` is never persisted —
  // autoExpand.ts's header on why that write, not the opening itself, was
  // D567's real defect.
  //
  // TWO ROW SOURCES, ONE OF THEM SILENT (code review 2026-08-28, finding 1).
  // The repo rows announce; the failures only ever hold the panel open. Passing
  // the repo roots alone meant the drain gate could not see the failures at
  // all, so pressing Update on the LAST repo row force-closed this panel over
  // failure rows the user was reading. `alsoDrawn` is the fix, and it keeps
  // D586's promise structural rather than configured: a failure is not in
  // `ids`, so there is no path from a failure to `setOverride("open")` — the
  // same guarantee the deleted second hook's `neverOpen`/`neverClose` pair used
  // to buy with two flags, now bought by feeding the hook what the panel
  // actually draws.
  //
  // PREFIXED PER SOURCE so a repo root and a job id can never collide inside
  // the hook's one seen set (a collision would put an unseen row in `prev` and
  // swallow its arrival). They are different namespaces today; the prefix means
  // nobody has to keep checking that.
  const { autoOpen, autoClose, acknowledge, forceClose } = useAutoExpandOnNew(
    [
      ...visible.map((row) => `repo:${row.repo.root}`),
      // Announceable, unlike the failures below: a pairing is the one event
      // here the user is usually WAITING on (they just scanned the QR).
      ...pairings.map((p) => `pair:${p.id}`),
    ],
    collapsed,
    ready,
    { alsoDrawn: failed.map((job) => `job:${job.id}`) },
  );
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
    if (collapsed === wantOpen) setCollapsed(!wantOpen);
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
      pairings={pairings}
      onPairingGone={onPairingGone}
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
  const { repos, pairings, setPairings, settled, refresh } = useRepoUpdates();
  const rows = repoRows(repos);
  const { dismissed, dismissOne, dismissAll } = useDismissed();
  const pairingGone = useCallback(
    (id: string) => setPairings((list) => list.filter((p) => p.id !== id)),
    [setPairings],
  );

  return (
    <RepoUpdatesDockView
      rows={rows}
      dismissed={dismissed}
      ready={settled}
      failed={failed}
      pairings={pairings}
      onPairingGone={pairingGone}
      onFailedPatch={onFailedPatch}
      onDismiss={dismissOne}
      onDismissAll={dismissAll}
      onDone={() => refresh()}
    />
  );
}
