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
// A FAILING row's SEVERITY (critical/warning) has no chip next to the
// label — it is told apart three other ways: the row's left rail and ground
// tint (app-doctor.css's `.appdoc-row-sev-*`), and the state mark's SHAPE
// (an alert circle for critical, a triangle for warning — `StateMark`
// below). Colour is therefore never the only carrier. The word itself, for a
// screen reader, lives in `rowStateAccessibleLabel` (appdoctor-lib.ts), which
// feeds the state mark's own `aria-label`/`title`.
//
// That accessible label is deliberately a SEPARATE helper from
// `rowStateDetailText`, which feeds `rowVisibleDetailText` below — the
// visible `.appdoc-detail` line. Folding the two into one string would put
// the severity word back on screen inside the detail line ("Critical —
// Failed — <detail>") — see appdoctor-lib.ts's comment on the two functions
// for why they must stay split. The visible line also drops the bare
// "Failed" word a settled fact failure would otherwise carry — the left
// rail, ground tint and state mark shape already say a row failed, three
// times over — and drops entirely for a passing row, since every checklist
// label is already a complete statement on its own. `rowVisibleDetailText`
// only prints `rowStateDetailText`'s output when it is a candidate's "N to
// review" count, real information the detail sentence does not otherwise
// carry.
//
// ONE GEOMETRY SCALE, shared by the section cards and the rows inside them:
// the card is `rounded-xl` (14px) and clips its own corners, its content pad
// is `p-1` (4px), and every row — passing, failing or skipped — is
// `rounded-lg px-3 py-2` (10px). 4 + 10 = 14, so a row's corner is
// concentric with the card's, and no row's box differs from its neighbours'.
// A failing row differs from a passing one ONLY by its ground tint and its
// left severity rail (app-doctor.css's `.appdoc-row-sev-*`), both drawn
// inside that same box — never by a different radius, pad or inset.
//
// Per-row Fix/Review creates ONE task on just that row; the footer's "Fix
// all" creates one task covering every currently failing row at once. Both
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
import { Check, CircleAlert, CircleMinus, CirclePlay, TriangleAlert } from "lucide-react";
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
  rowStateAccessibleLabel,
  rowVisibleDetailText,
  SECTION_LABEL,
  sectionStartsOpen,
  sectionSummary,
  sortByAttention,
  splitFindings,
  summaryLine,
  tasksTabUrl,
} from "./appdoctor-lib";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Card, CardContent } from "@platform/shadcn/ui/card";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { appLandingUrl } from "@platform/lib/appLanding";
import { navigateUrl } from "@platform/lib/router";
import { announceTasksChanged } from "@platform/lib/tasksChanged";
import { basename } from "@platform/lib/format";

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

// The one box every row wears, whatever its state — see this file's header
// comment on the shared geometry scale.
const ROW_BOX = "flex items-start gap-2.5 rounded-lg px-3 py-2";

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
              variant="outline"
              size="sm"
              title="An App Doctor task for this row is already running — listed under the app's Tasks tab"
              onClick={() => onFix(check)}
            >
              Fix in progress
            </Button>
          ) : (
            <Button
              variant="outline"
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
  // One disclosure level, keyed by section id, in plain component state (no
  // localStorage — the report is fetched fresh on every open, so there is
  // nothing to restore across opens). Seeded from `sectionStartsOpen` the
  // moment the report lands (below) and then left alone: the seeding effect
  // only ever fills in a key that isn't there yet, so a section the user has
  // since toggled by hand keeps that choice for the life of the dialog
  // instead of being reseeded out from under them on some later render.
  const [sectionOpen, setSectionOpen] = useState<Record<string, boolean>>({});
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

  // Seeds `sectionOpen` from the report the moment it lands, filling in only
  // the sections not already present — see the state declaration above for
  // why that matters.
  useEffect(() => {
    if (!report) return;
    setSectionOpen((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const group of groupBySection(report.checks)) {
        if (!(group.section in next)) {
          next[group.section] = sectionStartsOpen(group.checks);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [report]);

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

  const toggleSection = (section: string) =>
    setSectionOpen((prev) => ({ ...prev, [section]: !prev[section] }));

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
      <DialogContent className="grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden sm:max-w-[620px] max-h-[80vh]">
        <DialogHeader>
          <DialogTitle>{"App Doctor — " + (basename(dir) || dir)}</DialogTitle>
        </DialogHeader>
        <div className="flex min-h-0 flex-col gap-2 overflow-y-auto">
          <ErrorBanner>{error}</ErrorBanner>
          {report === null ? (
            <SkeletonLines rows={6} />
          ) : (
            <>
              <p className="appdoc-summary">{summaryLine(report.checks)}</p>
              {groupBySection(report.checks).map((group) => {
                const isOpen = sectionOpen[group.section] ?? sectionStartsOpen(group.checks);
                const listId = "appdoc-list-" + group.section;
                const sorted = sortByAttention(group.checks);
                return (
                  // `gap-0 py-0` because the header button and the row list
                  // carry their own pads; the card contributes the edge, the
                  // ground and the 14px radius its rows are cut to fit.
                  <Card key={group.section} className="gap-0 bg-muted/40 py-0">
                    <h3 className="m-0">
                      {/* The disclosure control: a full-width button carrying
                          `aria-expanded`/`aria-controls` so the section's own
                          `<ul>` can be toggled. It spans the card's full
                          width so the whole top edge is the hit target, and
                          its text indent (px-4) matches a row's own (the
                          card's p-1 content pad plus the row's px-3), so the
                          heading and the labels under it share one left edge.
                          The focus ring is inset, because the button meets
                          the card's clipped edge on three sides. */}
                      <button
                        type="button"
                        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-[13px] font-medium text-foreground [font-family:inherit] hover:bg-foreground/[0.04] focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--accent)]"
                        aria-expanded={isOpen}
                        aria-controls={listId}
                        onClick={() => toggleSection(group.section)}
                      >
                        <span className="flex-none">
                          {SECTION_LABEL[group.section] ?? group.section}
                        </span>
                        {/* Only a COLLAPSED section needs its contents counted —
                            an open one has the rows themselves right below, and
                            a count sitting above them is a second telling of
                            what the reader can already see. */}
                        {!isOpen && (
                          <span className="min-w-0 flex-1 truncate text-right text-xs font-normal text-muted-foreground">
                            {sectionSummary(group.checks)}
                          </span>
                        )}
                      </button>
                    </h3>
                    {isOpen && (
                      <CardContent className="p-1">
                        <ul className="m-0 flex list-none flex-col gap-0.5 p-0" id={listId}>
                          {sorted.map((c) => (
                            <CheckRow
                              key={c.id}
                              check={c}
                              busy={busy}
                              otherTaskLive={!!liveTask && !c.task}
                              onFix={fixRow}
                            />
                          ))}
                        </ul>
                      </CardContent>
                    )}
                  </Card>
                );
              })}
            </>
          )}
        </div>
        <DialogFooter>
          {liveTask ? (
            <Button
              variant="accent"
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
              variant="accent"
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
              {busy ? "Creating task…" : "Fix all"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default AppDoctorModal;
