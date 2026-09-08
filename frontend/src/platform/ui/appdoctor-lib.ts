// The pure half of the App Doctor dialog (AppDoctorModal.tsx) and its two
// header entry points (shell/AppPage.tsx, apps/explorer/Preview.tsx): what the
// summary line says, how a checklist groups into sections, which severity a
// row's failure counts as once a candidate's unreviewed status discounts it,
// how many findings a row draws, and the address of the app's Tasks tab. Split
// out for the same reason modal/dirty-guard.ts is — the chassis renders
// through a portal, which react-test-renderer cannot mount, so the decisions
// worth pinning live where a test can call them.
//
// There is no visible severity chip in the dialog any more (owner request —
// colour, the row's ground/rail and the state mark's shape already carry
// severity for a sighted reader). Severity used to leak back onto the screen
// once already: the chip's removal folded the word into `rowStateLabel`, and
// AppDoctorModal.tsx fed that SAME string to both the state mark's
// `aria-label`/`title` (fine — screen readers should still hear it) AND the
// row's visible detail line (not fine — the sentence a sighted reader sees
// then read "Critical — Failed — <detail>", the exact words the chip's
// removal was supposed to erase). `rowStateAccessibleLabel` and
// `rowStateDetailText` below are the fix: two names, two callers, so a future
// reader doesn't fold them back into one and reintroduce the leak.
import { encodeFsPathSegments } from "@platform/lib/router";
import type { AppCheck, AppCheckFinding, AppCheckState, Severity } from "@platform/lib/api";

// A long finding list is a report, not a UI: past this many the rest are
// counted rather than drawn, and the fix task sees all of them regardless.
export const MAX_FINDINGS_SHOWN = 12;

export const STATE_LABEL: Record<AppCheckState, string> = {
  pass: "Passed",
  fail: "Failed",
  skip: "Not checked",
  unrun: "Not run yet",
};

// Worst first — every ranking below (the header dot, the summary line, a
// chip's own ordering) reads off this single list rather than a second
// hardcoded ordering. The server sends the same order in `report.severities`;
// this copy is the one the two header entry points and the modal actually
// call against, so it exists regardless of whether a report has loaded yet.
export const SEVERITY_ORDER: readonly Severity[] = ["critical", "warning", "suggested"];

export const SEVERITY_LABEL: Record<Severity, string> = {
  critical: "Critical",
  warning: "Warning",
  suggested: "Suggested",
};

/** A row's severity for the purposes of ANY reduction across rows (the
 *  header dot, is-this-worse-than-that) — a CANDIDATE row's severity capped
 *  at "warning" even when the checklist lists it as "critical" (`secrets`).
 *  A candidate is unreviewed by definition (see app_doctor.py's module
 *  docstring and its measurement against a real workspace), so it must never
 *  read as urgently as a settled failure. This is the one place that
 *  discount happens — everything that ranks severities calls this rather
 *  than reading `check.severity` directly. */
export function effectiveSeverity(check: AppCheck): Severity {
  return check.kind === "candidate" && check.severity === "critical"
    ? "warning"
    : check.severity;
}

/** The worst FAILING severity across `checks` (via `effectiveSeverity`), or
 *  null when nothing failed — a clean report, or a report with only
 *  skip/unrun rows. The one reduction the header dot on BOTH surfaces and the
 *  modal read; do not re-derive it in a component. */
export function worstSeverity(checks: AppCheck[]): Severity | null {
  let worst: Severity | null = null;
  for (const c of checks) {
    if (c.state !== "fail") continue;
    const sev = effectiveSeverity(c);
    if (worst === null || SEVERITY_ORDER.indexOf(sev) < SEVERITY_ORDER.indexOf(worst)) {
      worst = sev;
    }
  }
  return worst;
}

/** The header dot's `title`/`aria-label` — colour is never the only carrier,
 *  so every state this can be in has words. `checks === null` is "the report
 *  has not landed yet" (the fetch-after-paint window, or a fetch that failed
 *  or is still running): the dot reads as unknown, not as clean. */
export function severityDotLabel(checks: AppCheck[] | null): string {
  if (checks === null) return "App Doctor: not checked yet";
  const failing = checks.filter((c) => c.state === "fail");
  if (failing.length === 0) return "App Doctor: nothing to fix";
  const counts = SEVERITY_ORDER.map((sev) => ({
    sev,
    n: failing.filter((c) => effectiveSeverity(c) === sev).length,
  })).filter((x) => x.n > 0);
  return (
    "App Doctor: " +
    counts.map((x) => `${x.n} ${SEVERITY_LABEL[x.sev].toLowerCase()}`).join(", ")
  );
}

/** Rows grouped into their sections, server order preserved (the server
 *  already emits `checks` in section order — essentials, then sharing — so
 *  this only has to notice where one section's run ends and the next
 *  begins, never sort). */
export function groupBySection(
  checks: AppCheck[],
): { section: string; checks: AppCheck[] }[] {
  const out: { section: string; checks: AppCheck[] }[] = [];
  for (const c of checks) {
    const last = out[out.length - 1];
    if (last && last.section === c.section) last.checks.push(c);
    else out.push({ section: c.section, checks: [c] });
  }
  return out;
}

export const SECTION_LABEL: Record<string, string> = {
  essentials: "Essentials",
  sharing: "Sharing",
};

/** The per-row action button's label: a CANDIDATE row asks the session to
 *  judge each finding first (Review), a FACT row asks it to fix outright
 *  (Fix) — see app_doctor.doctor_prompt's own triage-vs-fix split. */
export function rowActionLabel(check: AppCheck): "Fix" | "Review" {
  return check.kind === "candidate" ? "Review" : "Fix";
}

/** A failing row's own state word — feeds both `rowStateAccessibleLabel`
 *  below and, through it, `rowVisibleDetailText`'s prefix for the VISIBLE
 *  detail line (`.appdoc-detail`, AppDoctorModal.tsx) — never the severity. A
 *  candidate never reads as a settled failure ("Failed") — it reads as "N to
 *  review", because the pattern that flagged it has not been judged yet.
 *  Every other state reads as `STATE_LABEL` already does. Note that a
 *  settled fact failure's word here IS still "Failed", even though
 *  `rowVisibleDetailText` chooses not to print it on screen any more — this
 *  function's job is naming the state, not deciding what is worth showing.
 *
 *  This does NOT say the row's severity, on purpose: severity used to live in
 *  a visible chip (`.appdoc-sev`) next to the label, and when that chip was
 *  removed (owner request — colour, the rail and the state mark's SHAPE
 *  already carry severity for a sighted reader) an earlier pass folded the
 *  severity word into this same string, which put it right back on the
 *  screen inside the detail line ("Critical — Failed — <detail>") — the exact
 *  words removing the chip was supposed to get rid of. Severity belongs only
 *  in `rowStateAccessibleLabel` below, which feeds the state mark's
 *  `aria-label`/`title`, not this one. */
export function rowStateDetailText(check: AppCheck): string {
  if (check.state !== "fail") return STATE_LABEL[check.state];
  return check.kind === "candidate" ? `${check.findings.length} to review` : STATE_LABEL.fail;
}

/** A failing row's ACCESSIBLE name — feeds `.appdoc-state`'s `aria-label`/
 *  `title` (AppDoctorModal.tsx) only, never the visible detail line. This is
 *  the one place that still names severity in words at all: with the chip
 *  gone, this is the only place a screen reader hears
 *  "critical"/"warning"/"suggested". Built on `rowStateDetailText` so the two
 *  strings never drift apart on the state half — they differ by exactly the
 *  severity prefix. Uses `check.severity` (the checklist's own severity), not
 *  `effectiveSeverity` — that discount only applies to cross-row reductions
 *  (the header dot, worstSeverity); a row naming itself always says what the
 *  checklist actually found. */
export function rowStateAccessibleLabel(check: AppCheck): string {
  if (check.state !== "fail") return STATE_LABEL[check.state];
  return `${SEVERITY_LABEL[check.severity]} — ${rowStateDetailText(check)}`;
}

/** The visible `.appdoc-detail` line's full text (AppDoctorModal.tsx) — the
 *  checklist's own detail sentence, prefixed with `rowStateDetailText` only
 *  when that prefix carries information the sentence does not already: a
 *  candidate's "N to review" count. A settled fact failure's prefix is just
 *  `STATE_LABEL.fail` ("Failed"), and printing that added nothing the row
 *  was not already saying three other ways — the left rail and ground tint
 *  (app-doctor.css's `.appdoc-row-sev-*`) and the state mark's shape
 *  (StateIcon) — while stacking a third em dash onto an already
 *  dash-heavy sentence (owner request: drop it). `rowStateDetailText` itself
 *  still returns "Failed" for that row, unchanged — `rowStateAccessibleLabel`
 *  above needs it there to build a screen reader's "Critical — Failed"; only
 *  this visible-line function chooses not to print it. */
export function rowVisibleDetailText(check: AppCheck): string {
  if (check.state === "fail" && check.kind === "candidate") {
    return `${rowStateDetailText(check)} — ${check.detail}`;
  }
  return check.detail;
}

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

/** The sentence above the checklist: severity-first for what failed (a FACT
 *  row — nothing here is "the pattern might be wrong about this"), then how
 *  many candidate rows are still unreviewed, then how many rows the doctor
 *  could not answer at all. Never claims a skipped or unreviewed row as a
 *  pass — the "everything passed" clause requires BOTH failingFacts and
 *  failingCandidates to be empty; gating it on failingFacts alone let an app
 *  with a failing `secrets` row (a candidate, not a fact) read "Every check
 *  this app can answer passed. 1 row to review." — a pass claim sitting
 *  right next to the very row that contradicts it. */
export function summaryLine(checks: AppCheck[]): string {
  const failingFacts = checks.filter((c) => c.state === "fail" && c.kind === "fact");
  const failingCandidates = checks.filter(
    (c) => c.state === "fail" && c.kind === "candidate",
  );
  const skipped = checks.filter((c) => c.state === "skip").length;

  const parts: string[] = [];
  if (failingFacts.length === 0 && failingCandidates.length === 0) {
    parts.push("Every check this app can answer passed.");
  } else if (failingFacts.length > 0) {
    const bySeverity = SEVERITY_ORDER.map((sev) => ({
      sev,
      n: failingFacts.filter((c) => c.severity === sev).length,
    })).filter((x) => x.n > 0);
    parts.push(
      bySeverity.map((x) => `${x.n} ${SEVERITY_LABEL[x.sev].toLowerCase()}`).join(", ") +
        ` to fix.`,
    );
  }
  if (failingCandidates.length > 0) {
    parts.push(
      `${failingCandidates.length} row${failingCandidates.length === 1 ? "" : "s"} to review.`,
    );
  }
  if (skipped > 0) {
    parts.push(`${skipped} could not be checked.`);
  }
  return parts.join(" ");
}
