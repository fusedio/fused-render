// App Doctor: the share-readiness checklist for one app folder, and the one
// button that hands the whole report to a Claude session.
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
// Nothing in this dialog decides whether a finding matters. That judgment
// belongs to the `fused-render-app-doctor` skill, and "Explain and fix" is how
// you get it — one task on the app's entry page, whose session runs the skill
// end to end and lands in the Claude pane. One task for the whole report, not
// one per row: the findings are about the same folder and often the same file,
// and two sessions rewriting one app at once is a merge nobody asked for.
//
// Shared by the shell and the explorer, so it spells its own routes rather than
// importing either app's helpers (an app may not import the shell — the same
// reason Preview.tsx spells `/apps/<folder>?_tab=tasks` by hand).
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getAppDoctor,
  runAppDoctor,
  type AppCheck,
  type AppCheckState,
  type AppDoctorReport,
} from "@platform/lib/api";
import {
  findingWhere,
  splitFindings,
  STATE_LABEL,
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
function StateIcon({ state }: { state: AppCheckState }) {
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
  if (state === "fail")
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 8v4M12 16h.01" />
      </svg>
    );
  return (
    <svg {...common}>
      <circle cx="12" cy="12" r="9" />
      <path d="M8 12h8" />
    </svg>
  );
}

function CheckRow({ check }: { check: AppCheck }) {
  const { shown, hidden } = splitFindings(check.findings);
  return (
    <li className={"appdoc-row appdoc-" + check.state}>
      <span
        className="appdoc-state"
        role="img"
        aria-label={STATE_LABEL[check.state]}
        title={STATE_LABEL[check.state]}
      >
        <StateIcon state={check.state} />
      </span>
      <div className="appdoc-text">
        <span className="appdoc-label">{check.label}</span>
        <span className="appdoc-detail">{check.detail}</span>
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

  const live = report?.task ?? null;

  const fix = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await runAppDoctor(dir);
      if (res.task) announceTasksChanged();
      if (res.task_error) throw new Error(res.task_error);
      // The Claude pane can only attach to a run it has the id of; without one
      // the task is stored but not yet running, and the Tasks tab lists it.
      navigateUrl(
        res.task?.run_id
          ? appLandingUrl(res.entry_html, res.task.run_id)
          : tasksTabUrl(dir),
      );
      onClose();
    } catch (e) {
      if (alive.current) {
        setError((e as Error).message);
        setBusy(false);
      }
    }
  };

  return (
    <Modal
      title={"App Doctor — " + (basename(dir) || dir)}
      onClose={onClose}
      // The fix task keeps running server-side whether or not this dialog is
      // open, so closing mid-create abandons nothing.
      busy={false}
      dialogClassName="appdoc-modal"
      width={620}
      footer={
        <>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => {
              setReport(null);
              void load();
            }}
            disabled={busy || report === null}
          >
            Re-run
          </button>
          {live ? (
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
              onClick={fix}
              disabled={busy || report === null || !report.entry}
              title={
                report && !report.entry
                  ? "A task has to land on a page, and this folder has no entry page yet"
                  : "Creates one task on the app's entry page: a session that runs the App Doctor skill, explains every finding and fixes what is safe to fix"
              }
            >
              {busy ? "Creating task…" : "Explain and fix"}
            </button>
          )}
        </>
      }
    >
      <ErrorBanner>{error}</ErrorBanner>
      {report === null ? (
        <SkeletonLines rows={6} />
      ) : (
        <>
          <p className="appdoc-summary">{summaryLine(report.checks)}</p>
          <ul className="appdoc-list">
            {report.checks.map((c) => (
              <CheckRow key={c.id} check={c} />
            ))}
          </ul>
        </>
      )}
    </Modal>
  );
}

export default AppDoctorModal;
