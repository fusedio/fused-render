// App Doctor: the share-readiness checklist for one app folder, grouped into
// sections and, within each section, ordered worst-first (appdoctor-lib.ts's
// `sortByAttention`) so a failing row never has to be scrolled to. Each
// failing row carries its own severity and its own fix action.
//
// It stands where the "Migrate to new version" button used to (the app page's
// header, the explorer's entry-page topbar) and it subsumes it: the stale
// `fused-api-version` tag is now ONE ROW of the checklist rather than a button
// of its own, because it was never the only thing wrong with an app about to
// be shared — a pasted key, a path that only resolves on the author's machine,
// a `__pycache__` swept along and an uncommitted working tree are all invisible
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
// (filled alert circle for critical, triangle for warning — StateIcon
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
  splitPassingTail,
  summaryLine,
  tasksTabUrl,
} from "./appdoctor-lib";
import { Modal } from "@platform/ui/modal/Modal";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { appLandingUrl } from "@platform/lib/appLanding";
import { navigateUrl } from "@platform/lib/router";
import { announceTasksChanged } from "@platform/lib/tasksChanged";
import { basename } from "@platform/lib/format";

// Inline rather than lucide: this dialog is rendered inside the explorer too,
// which draws its own icons and imports no icon library.
//
// A FAILING state also draws by severity, not just by colour: a critical
// failure is a filled alert circle, a warning is a triangle (the shape
// everyone already reads as "caution"). `severity` is only meaningful (and
// only passed) for `state === "fail"`; every other state ignores it.
function StateIcon({ state, severity }: { state: AppCheckState; severity?: Severity }) {
  const common = {
    width: 16,
    height: 16,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  if (state === "pass")
    return (
      <svg {...common}>
        <path d="M20 6 9 17l-5-5" />
      </svg>
    );
  if (state === "fail") {
    if (severity === "warning")
      return (
        <svg {...common}>
          <path d="M12 3.5 21.5 20h-19z" />
          <path d="M12 9.5v4M12 16.5h.01" />
        </svg>
      );
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 8v4M12 16h.01" />
      </svg>
    );
  }
  if (state === "unrun")
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="9" strokeDasharray="3 3" />
        <path d="M10 9l5 3-5 3z" fill="currentColor" stroke="none" />
      </svg>
    );
  return (
    <svg {...common}>
      <circle cx="12" cy="12" r="9" />
      <path d="M8 12h8" />
    </svg>
  );
}

// A disclosure's own chevron — inline for the same reason StateIcon above is:
// this dialog renders inside the explorer too, which imports no icon
// library. `open` only changes which CSS class draws the rotation
// (app-doctor.css's `.appdoc-chevron-open`); the SVG itself never changes.
function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={"appdoc-chevron" + (open ? " appdoc-chevron-open" : "")}
    >
      <path d="M9 6l6 6-6 6" />
    </svg>
  );
}

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
      className={
        "appdoc-row appdoc-" +
        check.state +
        (failing ? " appdoc-row-sev-" + check.severity : "")
      }
    >
      <span
        className="appdoc-state"
        role="img"
        aria-label={rowStateAccessibleLabel(check)}
        title={rowStateAccessibleLabel(check)}
      >
        <StateIcon state={check.state} severity={failing ? check.severity : undefined} />
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
                <code>{findingWhere(f)}</code>
                {/* Already masked server-side when it came off a secret
                    (app_check.py's `_mask`), so this is safe to draw. */}
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
      {/* Row-level sibling of `.appdoc-text`, NOT nested inside the label (an
          earlier build nested it there and regressed — see this file's
          header comment and app-doctor.css's comment on
          `.appdoc-row-actions` for why: a 32px-tall button inside the
          label's own 20px line box inflated that line box, so a failing
          row's label-to-detail gap read wider than a passing row's and no
          two rows shared a vertical rhythm). As a row-level column its own
          height never touches the label's line box; `.appdoc-row-actions`'s
          `height: 20px` + `align-items: center` (app-doctor.css) instead
          centres the button on the label's first line, the same line-box
          trick `.appdoc-state` uses. `flex: none` keeps it from being
          squeezed by `.appdoc-text` (flex: 1 1 auto, the one element that
          absorbs width pressure and wraps instead — see `.appdoc-label`).
          A row with no action reserves none of this width: `.appdoc-text`
          simply grows to fill it. */}
      {failing && (
        <div className="appdoc-row-actions">
          {check.task ? (
            <button
              type="button"
              className="btn btn-secondary appdoc-fix-btn"
              title="An App Doctor task for this row is already running — listed under the app's Tasks tab"
              onClick={() => onFix(check)}
            >
              Fix in progress
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-secondary appdoc-fix-btn"
              disabled={busy || otherTaskLive}
              title={
                otherTaskLive
                  ? "An App Doctor task for this app is already running on another row — listed under the app's Tasks tab"
                  : undefined
              }
              onClick={() => onFix(check)}
            >
              {rowActionLabel(check)}
            </button>
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
  // Two independent disclosure levels, both keyed by section id, both plain
  // component state (no localStorage — the report is fetched fresh on every
  // open, so there is nothing to restore across opens). `sectionOpen` is
  // seeded from `sectionStartsOpen` the moment the report lands (below) and
  // then left alone: the seeding effect only ever fills in a key that isn't
  // there yet, so a section the user has since toggled by hand keeps that
  // choice for the life of the dialog instead of being reseeded out from
  // under them on some later render. `passFoldOpen` needs no seeding — every
  // passing-tail fold starts collapsed, so a missing key already reads as
  // closed.
  const [sectionOpen, setSectionOpen] = useState<Record<string, boolean>>({});
  const [passFoldOpen, setPassFoldOpen] = useState<Record<string, boolean>>({});
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
  const togglePassFold = (section: string) =>
    setPassFoldOpen((prev) => ({ ...prev, [section]: !prev[section] }));

  return (
    <Modal
      title={"App Doctor — " + (basename(dir) || dir)}
      onClose={onClose}
      // The fix task keeps running server-side whether or not this dialog is
      // open, so closing mid-create abandons nothing.
      busy={false}
      width={620}
      footer={
        liveTask ? (
          <button
            type="button"
            className="btn btn-primary"
            title="An App Doctor task for this app is still running — listed under the app's Tasks tab"
            onClick={() => {
              navigateUrl(tasksTabUrl(dir));
              onClose();
            }}
          >
            Fix in progress
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-primary"
            onClick={fixAll}
            disabled={busy || report === null || !report.entry ||
              !report.checks.some((c) => c.state === "fail")}
            title={
              report && !report.entry
                ? "A task has to land on a page, and this folder has no entry page yet"
                : "Creates one task on the app's entry page covering every failing row: triages candidates and fixes what is safe to fix"
            }
          >
            {busy ? "Creating task…" : "Fix all"}
          </button>
        )
      }
    >
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
            // The trailing run of passes folds behind its own disclosure
            // ONLY when there is something else in the list for it to fold
            // behind — a section that is nothing but passes already reads as
            // one line at the section level (`sectionStartsOpen` keeps it
            // collapsed by default), so nesting a second "N passed" fold
            // inside it here would just repeat that same disclosure once
            // the user opens it by hand.
            const { rows, passing } = splitPassingTail(sorted);
            const showFold = rows.length > 0 && passing.length > 0;
            const foldOpen = passFoldOpen[group.section] ?? false;
            const foldListId = listId + "-passed";
            return (
              <div className="appdoc-section" key={group.section}>
                <h3 className="appdoc-section-title">
                  <button
                    type="button"
                    className="appdoc-section-btn"
                    aria-expanded={isOpen}
                    aria-controls={listId}
                    onClick={() => toggleSection(group.section)}
                  >
                    <ChevronIcon open={isOpen} />
                    <span className="appdoc-section-name">
                      {SECTION_LABEL[group.section] ?? group.section}
                    </span>
                    {/* Only a COLLAPSED section needs its contents counted —
                        an open one has the rows themselves right below, and
                        a count sitting above them is a second telling of
                        what the reader can already see. */}
                    {!isOpen && (
                      <span className="appdoc-section-summary">
                        {sectionSummary(group.checks)}
                      </span>
                    )}
                  </button>
                </h3>
                {isOpen && (
                  <ul className="appdoc-list" id={listId}>
                    {(showFold ? rows : sorted).map((c) => (
                      <CheckRow
                        key={c.id}
                        check={c}
                        busy={busy}
                        otherTaskLive={!!liveTask && !c.task}
                        onFix={fixRow}
                      />
                    ))}
                    {showFold && (
                      <li className="appdoc-fold">
                        <button
                          type="button"
                          className="appdoc-fold-btn"
                          aria-expanded={foldOpen}
                          aria-controls={foldListId}
                          onClick={() => togglePassFold(group.section)}
                        >
                          <ChevronIcon open={foldOpen} />
                          <span>{passing.length} passed</span>
                        </button>
                        {foldOpen && (
                          <ul className="appdoc-list appdoc-fold-list" id={foldListId}>
                            {passing.map((c) => (
                              <CheckRow
                                key={c.id}
                                check={c}
                                busy={busy}
                                otherTaskLive={!!liveTask && !c.task}
                                onFix={fixRow}
                              />
                            ))}
                          </ul>
                        )}
                      </li>
                    )}
                  </ul>
                )}
              </div>
            );
          })}
        </>
      )}
    </Modal>
  );
}

export default AppDoctorModal;
