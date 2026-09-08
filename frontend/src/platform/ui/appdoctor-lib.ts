// The pure half of the App Doctor dialog (AppDoctorModal.tsx): what the
// summary line says, how many findings a row draws, and the address of the
// app's Tasks tab. Split out for the same reason modal/dirty-guard.ts is —
// the chassis renders through a portal, which react-test-renderer cannot
// mount, so the decisions worth pinning live where a test can call them.
import { encodeFsPathSegments } from "@platform/lib/router";
import type { AppCheck, AppCheckFinding, AppCheckState } from "@platform/lib/api";

// A long finding list is a report, not a UI: past this many the rest are
// counted rather than drawn, and the fix task sees all of them regardless.
export const MAX_FINDINGS_SHOWN = 12;

export const STATE_LABEL: Record<AppCheckState, string> = {
  pass: "Passed",
  fail: "Failed",
  skip: "Not checked",
};

/** The app's Tasks tab. Spelled here, not imported from the shell's
 *  current-apps-lib: this dialog renders inside the explorer too, and an app
 *  may not import the shell. */
export function tasksTabUrl(dir: string): string {
  return "/apps/" + encodeFsPathSegments(dir) + "?_tab=tasks";
}

export function splitFindings(findings: AppCheckFinding[]): {
  shown: AppCheckFinding[];
  hidden: number;
} {
  const shown = findings.slice(0, MAX_FINDINGS_SHOWN);
  return { shown, hidden: findings.length - shown.length };
}

/** `path:line` for a finding, or just the path when it is about the folder
 *  rather than a line (the server sends line 0 for those). */
export function findingWhere(f: AppCheckFinding): string {
  return f.line ? `${f.path}:${f.line}` : f.path;
}

/** The sentence above the checklist.
 *
 *  "Every check this app can answer passed" rather than "all checks passed":
 *  a skipped row is not a pass, and a summary that counted it as one would
 *  claim the doctor looked at something it could not see. */
export function summaryLine(checks: AppCheck[]): string {
  const failed = checks.filter((c) => c.state === "fail").length;
  const skipped = checks.filter((c) => c.state === "skip").length;
  const head =
    failed === 0
      ? "Every check this app can answer passed."
      : `${failed} of ${checks.length} checks failed.`;
  return skipped > 0
    ? `${head} ${skipped} could not be checked.`
    : head;
}
