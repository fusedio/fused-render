// App Doctor: the share-readiness checklist for one app folder, grouped into
// sections and, within each section, ordered worst-first (appdoctor-lib.ts's
// `sortByAttention`) so a failing row never has to be scrolled to. Each
// failing row carries its own severity and its own fix action.
//
// It is the app page's "App Doctor" TAB (shell/AppPage.tsx, `AppDoctorPanel`
// below, mounted as `?_tab=doctor`) and a DIALOG in the explorer's entry-page
// topbar (`AppDoctorModal`, apps/explorer/EntryActionsMenu.tsx). Both are the
// same three parts — `useAppDoctorReport`, `AppDoctorChecklist`,
// `AppDoctorFixAllButton` — in a different chassis; and it subsumes the "Migrate to new version" action: the stale
// `fused-api-version` tag is ONE ROW of the checklist rather than a button of
// its own, because it is never the only thing wrong with an app about to be
// shared — a pasted key, a path that only resolves on the author's machine, a
// `__pycache__` swept along and an uncommitted working tree are all invisible
// from the outside and all worth knowing before you send someone a folder.
//
// EVERY ROW BUT ONE IS DETERMINISTIC (fused_render/app_doctor.py, which is
// the authority on what each check means): a row passed, failed, or could not
// run, answered afresh on every GET. The exception is an ON-DEMAND row
// (`check.ondemand`, today `cross-browser`, fused_render/app_doctor_ai.py): a
// Sonnet read of the view files that spends tokens, so it runs only when its
// own Check button is pressed (`useAppDoctorReport`'s `runCheck`) — and it
// runs as a TASK on the app's entry page (`check.check_task` while live: the
// Tasks tab lists it, the shell's finished-task notice fires for it), never
// as a call this component waits on, so a tab switch cannot lose it. Its
// verdict is cached server-side on a checksum of those files — a GET draws
// the cached verdict for free, and reads `unrun` ("Not run yet") once the app
// has changed under it. A settled on-demand row keeps a Re-check for the
// case the cache cannot see: the rubric itself moved on.
// A FAILING row is not all the same kind of finding, though — `kind: "fact"`
// (a file exists or it does not) is a settled failure with a Fix button;
// `kind: "candidate"` (`secrets`, `device-paths`: a pattern match that only
// LOCATES something to look at, never decides — see app_doctor.py's own
// measurement) reads as "N to review" with a Review button, and never counts
// toward the header dot above warning on its own (appdoctor-lib.ts's
// `effectiveSeverity`). Judging a candidate is the fix session's job, which is
// why its button says Review rather than Fix.
//
// A FAILING row's SEVERITY (critical/warning) is told by the state mark's HUE
// and its SHAPE together — an alert circle for critical, a triangle for
// warning (`StateMark` below) — so colour is never the only carrier, and in
// words by that mark's own `aria-label`. Nothing else in the row changes: no
// fill, no edge, no plate, no tag, so the checklist reads as one surface from
// top to bottom.
//
// That accessible label is deliberately a SEPARATE helper from
// `rowStateDetailText`, which feeds `rowVisibleDetailText` below — the
// visible `.appdoc-detail` line. Folding the two into one string would print
// the severity a second time on screen, inside the detail line ("Critical —
// Failed — <detail>") — see appdoctor-lib.ts's comment on the two functions
// for why they must stay split. The visible line also drops the bare
// "Failed" word a settled fact failure would otherwise carry — the mark's hue
// and shape already say a row failed — and drops
// entirely for a passing row, since every checklist label is already a
// complete statement on its own. `rowVisibleDetailText` only prints
// `rowStateDetailText`'s output when it is a candidate's "N to review"
// count, real information the detail sentence does not otherwise carry.
//
// THE HEADER carries the whole summary in one line (`readinessCount` and
// `readinessSentence`, appdoctor-lib.ts): how many rows want an answer, out
// of how many were asked. That is the only tally in the dialog — a SECTION is
// a muted label sharing the row's left inset and nothing else (no card, no
// disclosure, no count), because a count above rows the reader can see is one
// fact told twice.
//
// A SETTLED row — passed, skipped, or never asked — is one dim line at body
// weight with 4px less vertical pad, so a run of them reads as a block to
// skip rather than as items to read. The rows that want something keep the
// full box, the label's weight, the detail line, the findings and the action.
// Nothing else about the box differs: one radius, one left inset, one hover
// wash, whatever the state.
//
// Per-row Fix/Review creates ONE task on just that row; the footer's "Fix N
// issues" creates one task covering every currently failing row at once, and
// its note names the candidates in that N, since the task reads those rather
// than rewriting them (`reviewNote`, appdoctor-lib.ts). Both
// share the same one-live-fix-session-per-app rule server-side (409): two
// sessions rewriting one folder is a merge nobody asked for.
//
// THE REPORT IS FETCHED FRESH ON EVERY MOUNT. The dialog is mounted behind
// `{open && …}` and the tab is not `keepMounted`, so opening either again is
// itself a re-run. The tab has no close to reopen, so its footer also offers
// a "Re-run" (the hook's `load`); the dialog does not need one.
//
// Shared by the shell and the explorer, so it spells its own routes rather than
// importing either app's helpers (an app may not import the shell — the same
// reason Preview.tsx spells `/apps/<folder>?_tab=tasks` by hand).
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Check,
  CircleAlert,
  CircleMinus,
  CirclePlay,
  GitPullRequest,
  RotateCw,
  TriangleAlert,
  X,
} from "lucide-react";
import {
  getAppDoctor,
  postJson,
  runAppDoctorAll,
  runAppDoctorCheck,
  runAppDoctorOnDemand,
  type AppCheck,
  type AppCheckState,
  type AppDoctorReport,
  type Severity,
} from "@platform/lib/api";
import {
  findingWhere,
  gitRowFetchPending,
  groupBySection,
  rowActionLabel,
  failingCount,
  readinessCount,
  readinessSentence,
  reviewNote,
  rowStateAccessibleLabel,
  rowVisibleDetailText,
  SECTION_LABEL,
  showsOpenInGitAction,
  showsPullAction,
  sortByAttention,
  splitFindings,
  tasksTabUrl,
} from "./appdoctor-lib";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { appLandingUrl } from "@platform/lib/appLanding";
import { navigate, navigateUrl } from "@platform/lib/router";
import { announceAppDoctorChanged, announceTasksChanged } from "@platform/lib/tasksChanged";

// The Pull button's own mutation result — same minimal shape
// shell/RepoUpdatesDock.tsx's own `MutationResult` keeps local rather than
// exported, since neither surface needs the other's. Pull reuses that same
// `POST /api/git-upstream {action: "update", root}` endpoint (never a new
// one): by the time this row can show Pull at all, `behind > 0` was read
// from `git_upstream`'s own state cache, so that root is already a
// "known repo" the endpoint's own allowlist (`is_known_repo`) accepts.
type PullResult = { ok: boolean; reason?: string; message?: string };

// A FAILING state draws by severity, not just by colour: a critical failure
// is an alert circle, a warning is a triangle (the shape everyone already
// reads as "caution"). `severity` is only meaningful (and only passed) for
// `state === "fail"`; every other state ignores it. The mark's hue comes from
// its `.appdoc-state` wrapper (app-doctor.css), which keys off the row's own
// state/severity classes.
function StateMark({ state, severity }: { state: AppCheckState; severity?: Severity }) {
  const common = { size: 16, "aria-hidden": true } as const;
  if (state === "pass") return <Check {...common} />;
  if (state === "fail")
    return severity === "warning" ? <TriangleAlert {...common} /> : <CircleAlert {...common} />;
  if (state === "unrun") return <CirclePlay {...common} />;
  return <CircleMinus {...common} />;
}

// The one box every row wears — same radius, same inset, same hover wash,
// which says "this is the row your pointer is on" and never "this row
// failed". Only the vertical pad differs, and only by 4px: a row with
// something to do gets the fuller box, and a settled row is drawn tighter so
// a run of them reads as one quiet block the eye can skip.
const ROW_BOX = "flex items-start gap-2.5 rounded-lg px-3 hover:bg-foreground/[0.04]";

function CheckRow({
  check,
  busy,
  otherTaskLive,
  creating,
  anyChecking,
  pulling,
  onFix,
  onCheck,
  onFollowCheck,
  onPull,
  onDone,
  onOpenGit,
}: {
  check: AppCheck;
  busy: boolean;
  /** Open the running check task (its Claude pane, or the Tasks tab). */
  onFollowCheck: (check: AppCheck) => void;
  /** Some OTHER row (or "Fix all") already has a live task — a fix or a
   *  check — the server allows exactly one at a time, so pressing this row's
   *  own button would just 409. Disabled rather than hidden, with a title
   *  saying why. */
  otherTaskLive: boolean;
  /** THIS on-demand row's check task is being created (the press is in
   *  flight; the row does not carry `check_task` yet). */
  creating: boolean;
  /** Some on-demand row's check task is being created or is live — one at a
   *  time, so a second press never queues behind the first. */
  anyChecking: boolean;
  /** THIS row's Pull is in flight — `git`-only, never true for another row. */
  pulling: boolean;
  onFix: (check: AppCheck) => void;
  /** `force` is Re-check: run again although the cached verdict still matches. */
  onCheck: (check: AppCheck, force?: boolean) => void;
  onPull: (check: AppCheck) => void;
  /** Fires after a navigating action — "Open in git" included (B4,
   *  FIXES-round-1.md) — the same idiom `fixRow`/`followLive`/`runFix`
   *  already use to dismiss the dialog once the user has actually landed
   *  somewhere else. `undefined` from the tab, which has nothing to close. */
  onDone?: () => void;
  /** G1 (FIXES-round-3.md): "Open in git" no longer navigates to a separate
   *  page — it opens the CALLER's own right-hand sidebar (Preview's `_side`
   *  over a file, the listing pane's over a folder) and selects its Git tab,
   *  staying on the page the user was already looking at. Owned by the
   *  caller because only the caller (Listing.tsx, Preview.tsx) knows which
   *  `_side`-writer is its own — this component has no sidebar of its own to
   *  open. `undefined` where the surface HAS no such sidebar (AppPage.tsx's
   *  `AppDoctorPanel`: read-only about git, no Git tab exists there — see its
   *  own header — and a snapshot/panel pane, which owns no address bar to
   *  write `_side` on), in which case the row falls back to the old
   *  navigate-to-the-git-mode behaviour so the action still does something
   *  rather than silently no-op. */
  onOpenGit?: () => void;
}) {
  const { shown, hidden } = splitFindings(check.findings);
  const failing = check.state === "fail";
  // "Checking…" from the press until the verdict lands: the create call in
  // flight, then the server's own word that a check task is live on it.
  const checking = creating || !!check.check_task;
  const openInGit = showsOpenInGitAction(check);
  // An on-demand row always has something to press — Check when it has not
  // been run on this content, Re-check once it has — so it takes the fuller
  // box a row with an action wears, even when it passed. So does the `git`
  // row once it has resolved a real repo root: "Open in git" is worth
  // offering even on a PASSING row (there is nothing to fix, but there is
  // still somewhere to look).
  const hasAction = failing || check.ondemand || openInGit;
  return (
    <li
      className={cn(
        ROW_BOX,
        hasAction ? "py-[9px]" : "py-[5px]",
        "appdoc-row appdoc-" + check.state,
        failing && "appdoc-row-sev-" + check.severity,
      )}
    >
      <span
        className="appdoc-state"
        role="img"
        aria-label={rowStateAccessibleLabel(check)}
        title={rowStateAccessibleLabel(check)}
      >
        <StateMark state={check.state} severity={failing ? check.severity : undefined} />
      </span>
      <div className="appdoc-text">
        <span className="appdoc-label">{check.label}</span>
        {checking ? (
          <span className="appdoc-detail">
            {creating
              ? "Creating the check task…"
              : "Claude (Sonnet) is reading the app's view files — listed under the app's Tasks tab; you will be notified when it finishes"}
          </span>
        ) : (
          rowVisibleDetailText(check) !== "" && (
            <span className="appdoc-detail">{rowVisibleDetailText(check)}</span>
          )
        )}
        {shown.length > 0 && (
          // A deterministic row's findings are source lines: one nowrap
          // monospace line each. A model-backed row's are SENTENCES written
          // for the author — what a visitor would see go wrong, then what to
          // change — so they wrap, in the body face, with the fix as a
          // quieter second line (`.appdoc-findings-prose`, app-doctor.css).
          <ul className={cn("appdoc-findings", check.ondemand && "appdoc-findings-prose")}>
            {shown.map((f, i) => (
              <li key={f.rule + f.path + f.line + i}>
                {/* Some rules excerpt the path itself (`git`'s porcelain
                    lines are `M <path>`), so the where-column would print
                    it a second time. Where it repeats, the excerpt says it
                    already. Excerpts are masked server-side when they came
                    off a secret (app_check.py's `_mask`), so both branches
                    are safe to draw. */}
                {!f.excerpt.includes(f.path) && <code>{findingWhere(f)}</code>}
                <span className="appdoc-excerpt">{f.excerpt}</span>
                {f.fix && <span className="appdoc-fix">Fix: {f.fix}</span>}
              </li>
            ))}
            {hidden > 0 && (
              <li className="appdoc-more">
                and {hidden} more — the fix task sees all of them
              </li>
            )}
          </ul>
        )}
      </div>
      {/* Row-level sibling of `.appdoc-text`, NOT nested inside the label: a
          button is taller than the label's 20px line box, so nesting it there
          would inflate that line box and a failing row's label-to-detail gap
          would read wider than a passing row's, breaking the shared vertical
          rhythm. As a row-level column its own height never touches the
          label's line box; `.appdoc-row-actions`'s `height: 20px` +
          `align-items: center` (app-doctor.css) instead centres the button on
          the label's first line, the same line-box trick `.appdoc-state`
          uses. `flex: none` keeps it from being squeezed by `.appdoc-text`
          (flex: 1 1 auto, the one element that absorbs width pressure and
          wraps instead — see `.appdoc-label`). A row with no action reserves
          none of this width: `.appdoc-text` simply grows to fill it. */}
      {hasAction && (
        <div className="appdoc-row-actions">
          {/* An on-demand row that has no verdict for the app as it is now:
              the only thing to do is run it. Nothing to fix yet, so no
              Fix/Review beside it. */}
          {check.ondemand && check.state === "unrun" ? (
            check.check_task ? (
              // The running check: the button IS the way there — the Claude
              // pane on its run, or the Tasks tab — same as a row's "Fix in
              // progress" opens the fix session.
              <Button
                variant="secondary"
                size="sm"
                title="Claude is checking this app — open the running task"
                onClick={() => onFollowCheck(check)}
              >
                Open check
              </Button>
            ) : (
              <Button
                variant="secondary"
                size="sm"
                disabled={anyChecking || busy || otherTaskLive}
                title={
                  otherTaskLive
                    ? "An App Doctor task is editing this app — check once it has finished"
                    : "Creates a task that asks Claude (Sonnet, low effort) to read this app's .html/.css/.js against the cross-browser rubric — cached until the files change"
                }
                onClick={() => onCheck(check)}
              >
                {checking ? "Checking…" : "Check"}
              </Button>
            )
          ) : (
            <>
              {/* Prominent and FIRST — a repo behind origin is the one
                  finding here a person is likely to act on immediately, and
                  unlike Fix/Review it never needs a Claude session: it is a
                  fast-forward `git pull`, nothing to judge. Shown alongside
                  Fix, not instead of it — being behind origin and having
                  uncommitted work are independent facts about the same
                  folder (see appdoctor-lib.ts's `showsPullAction`). */}
              {showsPullAction(check) && (
                <Button
                  variant="default"
                  size="sm"
                  disabled={pulling || busy || otherTaskLive}
                  title="Fast-forward this repo to match origin"
                  onClick={() => onPull(check)}
                >
                  <GitPullRequest aria-hidden />
                  {pulling ? "Pulling…" : "Pull"}
                </Button>
              )}
              {/* D1 (FIXES-round-1.md): a secondary text button matching
                  Fix's own size/variant, sitting BEFORE Fix — "Repo in sync
                  [Open in git] [Fix]". Styling unchanged since D1 (G1,
                  FIXES-round-3.md — the user approved this look). Never a fix
                  action: it opens the IN-APP git mode (never an external
                  client — out of scope per the spec), so it draws quietly
                  even on a passing row — there is nothing to fix, only
                  somewhere to look. Previously a bare ghost icon-only
                  button, which read as a stray mark next to Fix's solid
                  pill.

                  G1: what "opens" changed. It used to `navigate()` to this
                  row's repo root in `_mode=git` — a whole separate page,
                  leaving `index.html` (or wherever the user was) behind. Per
                  the user's own words ("open in git should just ensure the
                  sidebar is not turned off and the git tab is selected. no
                  need to open separate page"), it now stays put and opens the
                  CALLER's own sidebar on its Git tab (`onOpenGit`) — the
                  row's `gitRoot` already decided WHICH repo this row is
                  about; the sidebar it opens is scoped to the folder/file the
                  user is already on, which is that same repo (Doctor never
                  renders for a path outside it). Where no such sidebar exists
                  (`onOpenGit` undefined — AppPage.tsx's read-only-about-git
                  tab, or a snapshot/panel pane with no address bar of its
                  own), falls back to the old cross-page navigation so the
                  action still does something instead of silently no-op'ing. */}
              {openInGit && check.gitRoot && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    if (onOpenGit) onOpenGit();
                    else navigate(check.gitRoot as string, { isDir: true, mode: "git" });
                    onDone?.();
                  }}
                >
                  Open in git
                </Button>
              )}
              {failing &&
                (check.task ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    title="An App Doctor task for this row is already running — listed under the app's Tasks tab"
                    onClick={() => onFix(check)}
                  >
                    Fix in progress
                  </Button>
                ) : (
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={busy || otherTaskLive || checking}
                    title={
                      otherTaskLive
                        ? "An App Doctor task for this app is already running on another row — listed under the app's Tasks tab"
                        : undefined
                    }
                    onClick={() => onFix(check)}
                  >
                    {rowActionLabel(check)}
                  </Button>
                ))}
              {/* A settled on-demand row: the cache invalidates itself when
                  the files change, so this exists for what it cannot see —
                  a rubric that moved on, or a verdict worth a second
                  opinion. Icon-only, so it never competes with Fix. */}
              {check.ondemand && (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  // Not while a fix session is live on this app: it is about
                  // to change the very files a re-check would read, so the
                  // verdict would be stale the moment it landed.
                  disabled={anyChecking || busy || !!check.task || otherTaskLive}
                  title={
                    checking
                      ? "Checking…"
                      : check.task || otherTaskLive
                        ? "An App Doctor task is editing this app — re-check once it has finished"
                        : "Re-check with Claude (Sonnet)"
                  }
                  aria-label="Re-check"
                  onClick={() => onCheck(check, true)}
                >
                  <RotateCw aria-hidden className={checking ? "animate-spin" : undefined} />
                </Button>
              )}
            </>
          )}
        </div>
      )}
    </li>
  );
}

// The state and actions, shared by the tab and the dialog. `onDone` fires
// after a fix task was created (the caller has already been navigated to the
// task or the Tasks tab) and when a row's live task is followed — the dialog
// closes itself on it; the tab has nothing to close and passes nothing.
export function useAppDoctorReport(dir: string, onDone?: () => void) {
  const [report, setReport] = useState<AppDoctorReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pulling, setPulling] = useState<string | null>(null);
  const alive = useRef(true);
  // The `git` row's own async remote fetch: at most one silent retry per
  // mount (see the effect below) — never a poll loop.
  const gitRetried = useRef(false);
  useEffect(() => {
    // Re-arm on every mount: a remount (or React's dev double-invoke under
    // StrictMode) would otherwise leave this false forever, and every
    // setReport/setError below would be skipped — the checklist stuck on
    // SkeletonLines with no error shown.
    alive.current = true;
    gitRetried.current = false;
    return () => {
      alive.current = false;
    };
  }, []);

  // Fetched fresh every time the owner mounts (see the header comment); a
  // re-run is this same call with the old report cleared so the skeleton
  // shows the run is happening.
  const load = useCallback(async () => {
    setError(null);
    setReport(null);
    try {
      const r = await getAppDoctor(dir);
      if (alive.current) setReport(r);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    }
  }, [dir]);

  useEffect(() => {
    void load();
  }, [load]);

  // Doctor never blocks the initial paint on the `git` row's async remote
  // fetch (git_upstream's own throttled background dispatch) — the panel
  // paints immediately with that row reading SKIP, "not checked yet". This
  // is the one place that notices when the fetch has actually landed,
  // without the person having to press Re-run themselves: a SINGLE delayed
  // re-ask, patching only the `git` row in place (never `load()`'s own
  // reset-to-null, which would re-skeleton the whole panel over one row's
  // late answer). Fires at most once per mount — a repo with no remote at
  // all reads exactly like a fetch still pending (appdoctor-lib.ts's
  // `gitRowFetchPending`), and would never resolve no matter how many times
  // this asked again.
  useEffect(() => {
    if (!report || gitRetried.current || !gitRowFetchPending(report.checks)) return;
    const timer = setTimeout(() => {
      // Set the ref only once the timer actually FIRES (B3, FIXES-round-1.md)
      // — not when the effect merely schedules it. `runCheck` on any OTHER
      // row calls `setReport` with a new object, which cancels this timer via
      // the cleanup below and re-runs the effect; setting the ref up front
      // would have already marked the retry "used" on the cancelled attempt,
      // stranding the git row on SKIP forever. Deferring the flag lets the
      // re-armed effect schedule a fresh timer instead.
      gitRetried.current = true;
      void (async () => {
        try {
          const fresh = await getAppDoctor(dir);
          const freshGit = fresh.checks.find((c) => c.id === "git");
          if (alive.current && freshGit) {
            setReport((cur) =>
              cur
                ? {
                    ...cur,
                    checks: cur.checks.map((c) =>
                      c.id === "git" ? { ...freshGit, task: c.task } : c,
                    ),
                  }
                : cur,
            );
          }
        } catch {
          // Silent: the row just keeps its current (SKIP) reading — the
          // tab/dialog's own Re-run still works if the person wants another
          // try right away.
        }
      })();
    }, 2000);
    return () => clearTimeout(timer);
  }, [report, dir]);

  // Any row's live task is the whole app's live task — the server allows
  // exactly one at a time, so whichever row (or "Fix all") is running is the
  // one the footer and every idle row's button must defer to.
  const liveTask = report?.checks.find((c) => c.task)?.task ?? null;

  const runFix = async (action: () => ReturnType<typeof runAppDoctorAll>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await action();
      if (res.task) announceTasksChanged();
      if (res.task_error) throw new Error(res.task_error);
      // The Claude pane can only attach to a run it has the id of; without one
      // the task is stored but not yet running, and the Tasks tab lists it.
      navigateUrl(
        res.task?.run_id ? appLandingUrl(res.entry_html, res.task.run_id) : tasksTabUrl(dir),
      );
      onDone?.();
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setBusy(false);
      }
    }
  };

  const fixRow = (check: AppCheck) => {
    if (check.task || liveTask) {
      navigateUrl(tasksTabUrl(dir));
      onDone?.();
      return;
    }
    void runFix(() => runAppDoctorCheck(dir, check.id));
  };

  const fixAll = () => void runFix(() => runAppDoctorAll(dir));

  // Pull: a fast-forward-only `git fetch && merge`, not a fix session — no
  // Claude task, no navigation away from the panel. Reuses the SAME
  // `POST /api/git-upstream {action: "update", root}` mutation
  // shell/RepoUpdatesDock.tsx's own Update button calls; that endpoint's
  // allowlist (`git_upstream.is_known_repo`) already accepts this root,
  // since the row could only be showing Pull because `git_upstream` itself
  // just reported it behind. On success (or failure) re-`load()`s the whole
  // report — a pull can change more than the one row (a `.gitignore` that
  // just arrived from origin could turn an uncommitted-path failure into a
  // pass, for instance), so a full reset-and-reload is correct here in a way
  // it would not be for the silent remote-fetch retry above.
  const pullRow = async (check: AppCheck) => {
    if (pulling || !check.gitRoot) return;
    setPulling(check.id);
    setError(null);
    try {
      const res = await postJson<PullResult>("/api/git-upstream", {
        action: "update",
        root: check.gitRoot,
      });
      if (!res.ok) {
        if (alive.current) setError(res.message || "pull failed");
      } else {
        await load();
      }
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setPulling(null);
    }
  };

  const followLive = () => {
    navigateUrl(tasksTabUrl(dir));
    onDone?.();
  };

  // The on-demand row's CHECK TASK. In flight is a fact the SERVER holds —
  // `check.check_task` is read off the task store on every GET — not a
  // component flag, so a tab switch, a reload or a second window all draw
  // the same "Checking…" and none of them can lose it. `creating` is only
  // the seconds between the press and the server's answer (the create call
  // waits for the task's run id); the moment the row comes back it carries
  // `check_task` and this clears.
  const [creating, setCreating] = useState<string | null>(null);
  const runCheck = async (check: AppCheck, force = false) => {
    if (creating) return;
    setCreating(check.id);
    setError(null);
    try {
      const res = await runAppDoctorOnDemand(dir, check.id, force);
      // A new row on the Tasks tab — the pulse that feeds the shell's task
      // notices and the sidebar's unread dot should notice it now, not on
      // its next idle tick.
      if (res.task) announceTasksChanged();
      // ...and the header dot's own hook (useAppDoctorChecks) refetches on
      // this: its answer now carries `check_task`, which is what starts ITS
      // poll — the one that outlives this panel, so a check that finishes
      // after a tab switch still moves the dot.
      announceAppDoctorChanged(dir);
      if (alive.current) {
        setReport((r) =>
          r
            ? {
                ...r,
                checks: r.checks.map((c) => (c.id === res.check.id ? res.check : c)),
              }
            : r,
        );
      }
      if (res.task_error) throw new Error(res.task_error);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setCreating(null);
    }
  };

  // While a check task is live, ask the server again every few seconds so
  // the verdict lands on the row the moment the task writes it — without
  // this, the row would say "Checking…" until the doctor was reopened. A
  // plain GET: a bounded folder walk, no tokens. The old report stays on
  // screen until the new one arrives (never through `load`, which blanks
  // the checklist to a skeleton). When the task is gone from the answer,
  // the header dot's own fetch (useAppDoctorChecks) is rung so it moves off
  // "clean" over a row that just went red. Keyed on the live task's id, so
  // the interval restarts only when the task changes, not on every render.
  const liveCheckId = report?.checks.find((c) => c.check_task)?.check_task?.id ?? null;
  useEffect(() => {
    if (!liveCheckId) return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const r = await getAppDoctor(dir);
        if (cancelled || !alive.current) return;
        setReport(r);
        if (!r.checks.some((c) => c.check_task?.id === liveCheckId)) {
          announceAppDoctorChanged(dir);
        }
      } catch {
        /* the next tick asks again; the row keeps saying "Checking…" meanwhile */
      }
    }, CHECK_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [dir, liveCheckId]);

  // Take the user to the running check: the Claude pane on its run when the
  // task has a run id (same landing the fix flow uses), else the Tasks tab
  // where the stored entry is listed.
  const followCheck = (check: AppCheck) => {
    const run = check.check_task?.run_id;
    navigateUrl(run && report?.entry ? appLandingUrl(report.entry, run) : tasksTabUrl(dir));
    onDone?.();
  };

  // A check task on any row (or one being created) holds the same
  // one-live-task-per-app gate a fix does, so the footer's "Fix all" must
  // read it as in-flight too — pressing it would just 409.
  const checkLive = creating !== null || !!liveCheckId;

  return {
    report,
    error,
    busy,
    liveTask,
    checkLive,
    load,
    fixRow,
    fixAll,
    followLive,
    creating,
    runCheck,
    followCheck,
    pulling,
    pullRow,
    onDone,
  };
}

// How often the checklist re-asks the server while a check task is live. A
// check is a Sonnet read of a few files — tens of seconds — so a few seconds
// between asks lands the verdict promptly without hammering a folder walk.
const CHECK_POLL_MS = 4_000;

type Report = ReturnType<typeof useAppDoctorReport>;

// The one-line summary: what the report amounts to, with the count at full
// foreground because it is the part worth finding again on a second look.
// The dialog renders it as its `DialogDescription` (so a screen reader hears
// it with the title); the tab renders it as a plain paragraph.
function SummaryText({ report }: { report: AppDoctorReport }) {
  return (
    <>
      <b>{readinessCount(report.checks)}</b> {readinessSentence(report.checks)}
    </>
  );
}

// The grouped checklist, skeleton while loading, error banner above.
function AppDoctorChecklist({
  report,
  error,
  busy,
  liveTask,
  fixRow,
  creating,
  runCheck,
  followCheck,
  pulling,
  pullRow,
  onDone,
  onOpenGit,
}: Report & {
  /** G1 — see CheckRow's own doc comment. Not part of `useAppDoctorReport`'s
   *  state (it knows nothing about any sidebar); threaded in separately by
   *  each caller of `AppDoctorChecklist`. */
  onOpenGit?: () => void;
}) {
  // Any row's live check task — one per app, like the fix task.
  const anyCheckLive = !!report?.checks.some((c) => c.check_task);
  return (
    <>
      <ErrorBanner>{error}</ErrorBanner>
      {report === null ? (
        <SkeletonLines rows={6} />
      ) : (
        groupBySection(report.checks).map((group) => (
          // A heading and its list, nothing around them. The heading shares
          // the row's left inset, so it sits on one edge with the rows under
          // it, and the gap the owner puts between the sections does the
          // grouping a box would otherwise be drawn for.
          <section key={group.section} className="flex min-w-0 flex-col">
            <h3 className="appdoc-section-label">
              {SECTION_LABEL[group.section] ?? group.section}
            </h3>
            <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
              {sortByAttention(group.checks).map((c) => (
                <CheckRow
                  key={c.id}
                  check={c}
                  busy={busy}
                  otherTaskLive={(!!liveTask && !c.task) || (anyCheckLive && !c.check_task)}
                  creating={creating === c.id}
                  anyChecking={creating !== null || anyCheckLive}
                  pulling={pulling === c.id}
                  onFix={fixRow}
                  onCheck={(check, force) => void runCheck(check, force)}
                  onFollowCheck={followCheck}
                  onPull={(check) => void pullRow(check)}
                  onDone={onDone}
                  onOpenGit={onOpenGit}
                />
              ))}
            </ul>
          </section>
        ))
      )}
    </>
  );
}

// The primary action: "Fix N issues" / "Fix in progress" / "Nothing to fix".
function AppDoctorFixAllButton({ report, busy, liveTask, checkLive, fixAll, followLive }: Report) {
  if (checkLive) {
    return (
      <Button
        variant="default"
        size="sm"
        disabled
        title="A check task is running on this app — fixes wait until its verdict lands"
      >
        Check in progress
      </Button>
    );
  }
  if (liveTask) {
    return (
      <Button
        variant="default"
        size="sm"
        title="An App Doctor task for this app is still running — listed under the app's Tasks tab"
        onClick={followLive}
      >
        Fix in progress
      </Button>
    );
  }
  return (
    <Button
      variant="default"
      size="sm"
      onClick={fixAll}
      disabled={
        busy ||
        report === null ||
        !report.entry ||
        !report.checks.some((c) => c.state === "fail")
      }
      title={
        report && !report.entry
          ? "A task has to land on a page, and this folder has no entry page yet"
          : "Creates one task on the app's entry page covering every failing row: triages candidates and fixes what is safe to fix"
      }
    >
      {busy
        ? "Creating task…"
        : report && failingCount(report.checks) > 0
          ? "Fix " +
            failingCount(report.checks) +
            (failingCount(report.checks) === 1 ? " issue" : " issues")
          : "Nothing to fix"}
    </Button>
  );
}

// The app page's tab body (shell/AppPage.tsx, `?_tab=doctor`). Same three
// rows as the dialog — summary, scrolling checklist, pinned footer — on the
// page's own ground with no box around it, the way the page's other panels
// sit. It always checks the LIVE folder: the version picker's snapshot is an
// extracted read-only tree, and there is nothing a fix task could do to it,
// so this panel ignores `_snapshot` rather than reporting on a copy.
export function AppDoctorPanel({ dir }: { dir: string }) {
  const r = useAppDoctorReport(dir);
  // G1: no `onOpenGit` here on purpose. This page (AppPage.tsx) is
  // deliberately read-only about git — no Git tab exists on it at all (see
  // that file's own header) — so there is no sidebar for "Open in git" to
  // open. CheckRow's fallback (the old cross-page navigate) is the correct,
  // sensible degrade for this surface, not a gap to fill in.
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-5 overflow-x-hidden overflow-y-auto">
        {r.report !== null && (
          <p className="appdoc-summary">
            <SummaryText report={r.report} />
          </p>
        )}
        <AppDoctorChecklist {...r} />
      </div>
      <div className="mt-4 flex flex-none items-center justify-between gap-3 border-t border-t-[var(--border)] pt-4">
        <span className="appdoc-foot-note">
          {r.report === null ? "" : reviewNote(r.report.checks)}
        </span>
        <div className="flex flex-none gap-2">
          <Button
            variant="outline"
            size="sm"
            // In flight = no report AND no error yet. A FAILED fetch also
            // leaves `report` null, and that is exactly when this button
            // is needed — so it is never gated on the report alone.
            disabled={r.busy || (r.report === null && r.error === null)}
            title="Run the checks again"
            onClick={() => void r.load()}
          >
            <RotateCw data-icon="inline-start" />
            Re-run
          </Button>
          <AppDoctorFixAllButton {...r} />
        </div>
      </div>
    </div>
  );
}

// The explorer's dialog (apps/explorer/EntryActionsMenu.tsx).
export function AppDoctorModal({
  dir,
  onClose,
  onOpenGit,
}: {
  /** The app FOLDER (canonical forward-slash), not its entry page. */
  dir: string;
  onClose: () => void;
  /** G1 (FIXES-round-3.md) — see CheckRow's own doc comment. Handed down by
   *  `useAppActionRows` (EntryActionsMenu.tsx), which gets it from whichever
   *  of Listing.tsx/Preview.tsx mounted it — each owns its own `_side`
   *  writer, this dialog owns none. `undefined` on a surface with no
   *  sidebar of its own (a snapshot/panel pane), where the row falls back to
   *  navigating instead. */
  onOpenGit?: () => void;
}) {
  const r = useAppDoctorReport(dir, onClose);
  return (
    // Always open while mounted: the caller renders this behind
    // `{open && …}`, so the only close this dialog can report is the user's.
    // The fix task keeps running server-side whether or not the dialog is
    // open, so closing mid-create abandons nothing.
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      {/* Three grid rows — head, scrolling checklist, footer — so the title
          and "Fix all" stay put while a long report scrolls between them.
          `minmax(0, 1fr)` on the middle row is what lets it actually shrink
          to the 80vh cap instead of pushing the footer off-screen. */}
      <DialogContent
        className="grid-rows-[auto_minmax(0,1fr)_auto] gap-4 overflow-hidden p-6 sm:max-w-[620px] max-h-[80vh]"
        showCloseButton={false}
      >
        {/* The close control is rendered here, inside the header row, rather
            than taken from `DialogContent`'s own absolutely-positioned one:
            in the header it sits ON the title's line, so the two read as one
            title bar instead of a heading with a button floating over the
            dialog's top corner. */}
        <DialogHeader className="gap-2">
          <div className="flex items-start justify-between gap-2">
            <DialogTitle className="font-semibold">App Doctor</DialogTitle>
            <DialogClose
              render={<Button variant="ghost" size="icon-sm" className="-mt-1 -mr-1" />}
            >
              <X aria-hidden />
              <span className="sr-only">Close</span>
            </DialogClose>
          </div>
          {r.report !== null && (
            <DialogDescription className="appdoc-summary">
              <SummaryText report={r.report} />
            </DialogDescription>
          )}
        </DialogHeader>
        <div className="flex min-h-0 min-w-0 flex-col gap-5 overflow-x-hidden overflow-y-auto">
          <AppDoctorChecklist {...r} onOpenGit={onOpenGit} />
        </div>
        {/* The footer's hairline is the dialog's own border colour, not the
            button ground's — it separates the list from the action without
            drawing a bright line across the dialog. */}
        <DialogFooter className="-mx-6 -mb-6 items-center gap-3 border-t border-t-[var(--border)] bg-transparent px-6 py-4 sm:justify-between">
          <span className="appdoc-foot-note">
            {r.report === null ? "" : reviewNote(r.report.checks)}
          </span>
          <div className="flex flex-none gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>
              Close
            </Button>
            <AppDoctorFixAllButton {...r} />
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default AppDoctorModal;
