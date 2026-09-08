// App Doctor: the share-readiness checklist for one app folder, grouped into
// sections and, within each section, ordered worst-first (appdoctor-lib.ts's
// `sortByAttention`) so a failing row never has to be scrolled to. Each
// failing row carries its own severity and its own fix action.
//
// It sits in the app page's header and in the explorer's entry-page topbar,
// and it subsumes the "Migrate to new version" action: the stale
// `fused-api-version` tag is ONE ROW of the checklist rather than a button of
// its own, because it is never the only thing wrong with an app about to be
// shared — a pasted key, a path that only resolves on the author's machine, a
// `__pycache__` swept along and an uncommitted working tree are all invisible
// from the outside and all worth knowing before you send someone a folder.
//
// EVERY ROW IS DETERMINISTIC (fused_render/app_doctor.py, which is the
// authority on what each check means): a row passed, failed, or could not run.
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
// THE READINESS STRIP above the list is the one thing in the dialog that
// spends anything: one tick per check, in the server's own check order, so
// the shape of the whole report is legible before a word of it is read (see
// the strip's own comment below and `.appdoc-strip` in app-doctor.css). It is
// also what pays for everything under it being plain — a SECTION is a muted
// label sharing the row's left inset and nothing else (no card, no
// disclosure, no tally), because the summary is already drawn and a count
// above rows the reader can see is one fact told twice.
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
// THE REPORT IS FETCHED FRESH ON EVERY OPEN. There is no cached report inside
// this component to refresh, which is why there is no Re-run button — closing
// and reopening the dialog (the caller mounts this behind `{open && …}`) is
// itself the re-run.
//
// Shared by the shell and the explorer, so it spells its own routes rather than
// importing either app's helpers (an app may not import the shell — the same
// reason Preview.tsx spells `/apps/<folder>?_tab=tasks` by hand).
import { useCallback, useEffect, useRef, useState } from "react";
import { Check, CircleAlert, CircleMinus, CirclePlay, TriangleAlert, X } from "lucide-react";
import {
  getAppDoctor,
  runAppDoctorAll,
  runAppDoctorCheck,
  type AppCheck,
  type AppCheckState,
  type AppDoctorReport,
  type Severity,
} from "@platform/lib/api";
import {
  findingWhere,
  groupBySection,
  rowActionLabel,
  failingCount,
  readinessCount,
  readinessSentence,
  reviewNote,
  rowStateAccessibleLabel,
  rowVisibleDetailText,
  SECTION_LABEL,
  sortByAttention,
  splitFindings,
  stripLabel,
  tickTone,
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
import { basename } from "@platform/lib/format";
import { cn } from "@platform/lib/utils";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { appLandingUrl } from "@platform/lib/appLanding";
import { navigateUrl } from "@platform/lib/router";
import { announceTasksChanged } from "@platform/lib/tasksChanged";

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
  onFix,
}: {
  check: AppCheck;
  busy: boolean;
  /** Some OTHER row (or "Fix all") already has a live task — the server
   *  allows exactly one at a time, so pressing this row's own button would
   *  just 409. Disabled rather than hidden, with a title saying why. */
  otherTaskLive: boolean;
  onFix: (check: AppCheck) => void;
}) {
  const { shown, hidden } = splitFindings(check.findings);
  const failing = check.state === "fail";
  return (
    <li
      className={cn(
        ROW_BOX,
        failing ? "py-[9px]" : "py-[5px]",
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
        {rowVisibleDetailText(check) !== "" && (
          <span className="appdoc-detail">{rowVisibleDetailText(check)}</span>
        )}
        {shown.length > 0 && (
          <ul className="appdoc-findings">
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
      {failing && (
        <div className="appdoc-row-actions">
          {check.task ? (
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
              disabled={busy || otherTaskLive}
              title={
                otherTaskLive
                  ? "An App Doctor task for this app is already running on another row — listed under the app's Tasks tab"
                  : undefined
              }
              onClick={() => onFix(check)}
            >
              {rowActionLabel(check)}
            </Button>
          )}
        </div>
      )}
    </li>
  );
}

export function AppDoctorModal({
  dir,
  onClose,
}: {
  /** The app FOLDER (canonical forward-slash), not its entry page. */
  dir: string;
  onClose: () => void;
}) {
  const [report, setReport] = useState<AppDoctorReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    // Re-arm on every mount: a remount (or React's dev double-invoke under
    // StrictMode) would otherwise leave this false forever, and every
    // setReport/setError below would be skipped — the dialog stuck on
    // SkeletonLines with no error shown.
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Fetched fresh every time this component mounts — the caller renders it
  // behind `{open && <AppDoctorModal …/>}`, so opening the dialog again is
  // itself the re-run; there is no cached report in here to go stale.
  const load = useCallback(async () => {
    setError(null);
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
      onClose();
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
      onClose();
      return;
    }
    void runFix(() => runAppDoctorCheck(dir, check.id));
  };

  const fixAll = () => void runFix(() => runAppDoctorAll(dir));

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
        <DialogHeader className="gap-3">
          <div className="flex items-start justify-between gap-2">
            <div className="flex min-w-0 flex-col gap-1">
              <DialogTitle className="font-semibold">App Doctor</DialogTitle>
              <DialogDescription className="appdoc-summary">
                {"Checked " + (basename(dir) || dir) + " just now."}
              </DialogDescription>
            </div>
            <DialogClose
              render={<Button variant="ghost" size="icon-sm" className="-mt-1 -mr-1" />}
            >
              <X aria-hidden />
              <span className="sr-only">Close</span>
            </DialogClose>
          </div>
          {/* The readiness strip: one tick per check, in the server's own
              check order, so the shape of the whole report reads before any
              row does. It is the one thing in the dialog that spends colour
              on a passing row, and it is why nothing below it needs a card,
              a tally or a tag — the summary is already drawn. The ticks are
              decorative on their own, so the strip carries `stripLabel`'s
              counts for a reader who cannot see them. */}
          {report !== null && (
            <div className="appdoc-strip">
              <div className="appdoc-ticks" role="img" aria-label={stripLabel(report.checks)}>
                {report.checks.map((c) => (
                  <span key={c.id} className={"appdoc-tick appdoc-tick-" + tickTone(c)} />
                ))}
              </div>
              <p className="appdoc-strip-read">
                <b>{readinessCount(report.checks)}</b> {readinessSentence(report.checks)}
              </p>
            </div>
          )}
        </DialogHeader>
        <div className="flex min-h-0 min-w-0 flex-col gap-2 overflow-x-hidden overflow-y-auto">
          <ErrorBanner>{error}</ErrorBanner>
          {report === null ? (
            <SkeletonLines rows={6} />
          ) : (
            <>
              {groupBySection(report.checks).map((group) => (
                // A heading and its list, nothing around them. The heading is
                // inset to the LABEL column, not to the row's own edge, so
                // the section name and every label under it share one left
                // edge and the state marks hang in the gutter beside them —
                // that one column is what does the grouping a box would
                // otherwise be drawn for.
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
                        otherTaskLive={!!liveTask && !c.task}
                        onFix={fixRow}
                      />
                    ))}
                  </ul>
                </section>
              ))}
            </>
          )}
        </div>
        {/* The footer's hairline is the dialog's own border colour, not the
            button ground's — it separates the list from the action without
            drawing a bright line across the dialog. */}
        <DialogFooter className="-mx-6 -mb-6 items-center gap-3 border-t border-t-[var(--border)] bg-transparent px-6 py-4 sm:justify-between">
          <span className="appdoc-foot-note">
            {report === null ? "" : reviewNote(report.checks)}
          </span>
          <div className="flex flex-none gap-2">
          {liveTask ? (
            <Button
              variant="default"
              size="sm"
              title="An App Doctor task for this app is still running — listed under the app's Tasks tab"
              onClick={() => {
                navigateUrl(tasksTabUrl(dir));
                onClose();
              }}
            >
              Fix in progress
            </Button>
          ) : (
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
          )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default AppDoctorModal;
