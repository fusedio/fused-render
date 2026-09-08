// The App Doctor dialog's pure decisions (appdoctor-lib.ts). The dialog itself
// mounts through the modal chassis' portal, which react-test-renderer cannot
// render — same split, and same reason, as modal/dirty-guard.test.ts.
import { expect, test } from "bun:test";
import type { AppCheck, AppCheckFinding } from "@platform/lib/api";

// `tasksTabUrl` encodes through lib/router, which reads `location` and
// `history` at module init (its legacy-URL rewrite). A minimal stand-in
// installed before the dynamic import is enough, and is preferred over
// `mock.module("…/router")` — bun's module mocks are process-wide, so stubbing
// a module this widely imported would reach unrelated files.
//
// The globals are PUT BACK the moment the import resolves, for the same
// reason: `bun test` runs every file in one process, and a leaked `location`
// breaks whoever else reads it (appEntry.test.ts does). Router has already
// read them by then and never looks again.
const before = {
  location: Reflect.getOwnPropertyDescriptor(globalThis, "location"),
  history: Reflect.getOwnPropertyDescriptor(globalThis, "history"),
};
Object.assign(globalThis, {
  location: { pathname: "/", search: "", href: "http://localhost/" },
  history: { state: null, replaceState() {} },
});
const lib = await import("./appdoctor-lib");
for (const key of ["location", "history"] as const) {
  const desc = before[key];
  if (desc) Reflect.defineProperty(globalThis, key, desc);
  else Reflect.deleteProperty(globalThis, key);
}

const {
  effectiveSeverity,
  findingWhere,
  groupBySection,
  MAX_FINDINGS_SHOWN,
  rowActionLabel,
  rowStateAccessibleLabel,
  rowStateDetailText,
  severityDotLabel,
  SEVERITY_LABEL,
  splitFindings,
  STATE_LABEL,
  summaryLine,
  tasksTabUrl,
  worstSeverity,
} = lib;

const check = (
  id: string,
  state: AppCheck["state"],
  extra: Partial<AppCheck> = {},
): AppCheck => ({
  id,
  section: "essentials",
  severity: "warning",
  kind: "fact",
  label: id,
  state,
  detail: "",
  findings: [],
  task: null,
  ...extra,
});

const finding = (path: string, line = 0): AppCheckFinding => ({
  rule: "r",
  path,
  line,
  excerpt: "x",
});

test("a clean report does not claim to have checked what it skipped", () => {
  expect(summaryLine([check("a", "pass"), check("b", "pass")])).toBe(
    "Every check this app can answer passed.",
  );
  // The whole point of the wording: two passes and a skip is NOT "all clear".
  expect(summaryLine([check("a", "pass"), check("b", "skip")])).toBe(
    "Every check this app can answer passed. 1 could not be checked.",
  );
});

test("a failing fact row is counted by severity, not as a flat total", () => {
  const checks = [
    check("a", "fail", { severity: "critical" }),
    check("b", "pass"),
    check("c", "fail", { severity: "warning" }),
  ];
  expect(summaryLine(checks)).toBe("1 critical, 1 warning to fix.");
});

test("a failing candidate row reads as 'to review', never merged into the fact tally", () => {
  const checks = [
    check("secrets", "fail", { severity: "critical", kind: "candidate" }),
    check("readme", "pass"),
  ];
  expect(summaryLine(checks)).toBe(
    "Every check this app can answer passed. 1 row to review.",
  );
});

test("facts, candidates and skips all show up in one summary", () => {
  const checks = [
    check("a", "fail", { severity: "warning" }),
    check("secrets", "fail", { severity: "critical", kind: "candidate" }),
    check("b", "skip"),
  ];
  expect(summaryLine(checks)).toBe(
    "1 warning to fix. 1 row to review. 1 could not be checked.",
  );
});

test("a long finding list is capped and the rest counted", () => {
  const many = Array.from({ length: MAX_FINDINGS_SHOWN + 5 }, (_, i) =>
    finding(`f${i}.py`, i + 1),
  );
  const { shown, hidden } = splitFindings(many);
  expect(shown).toHaveLength(MAX_FINDINGS_SHOWN);
  expect(hidden).toBe(5);
  // A list that fits is drawn whole, with nothing to count.
  expect(splitFindings(many.slice(0, 3))).toEqual({
    shown: many.slice(0, 3),
    hidden: 0,
  });
});

test("a finding about the folder shows no line number", () => {
  // The server sends line 0 for a finding about the folder itself; `path:0`
  // would read as a real location and jump nowhere.
  expect(findingWhere(finding("__pycache__/"))).toBe("__pycache__/");
  expect(findingWhere(finding("app.py", 12))).toBe("app.py:12");
});

test("the tasks tab is the app page's own address, path-encoded", () => {
  expect(tasksTabUrl("/Users/me/Fused/local/my app")).toBe(
    "/apps/Users/me/Fused/local/my%20app?_tab=tasks",
  );
});

test("every state has a label, so no row renders a bare icon", () => {
  expect(Object.keys(STATE_LABEL).sort()).toEqual(["fail", "pass", "skip", "unrun"]);
  expect(Object.values(STATE_LABEL).every((v) => v.length > 0)).toBe(true);
});

// ------------------------------------------------------------- section grouping

test("rows group into their sections in server order, without sorting", () => {
  const checks = [
    check("secrets", "pass", { section: "essentials" }),
    check("entry", "pass", { section: "essentials" }),
    check("git", "pass", { section: "sharing" }),
    check("pushed", "pass", { section: "sharing" }),
  ];
  expect(groupBySection(checks)).toEqual([
    { section: "essentials", checks: [checks[0], checks[1]] },
    { section: "sharing", checks: [checks[2], checks[3]] },
  ]);
});

// ------------------------------------------------------------------ severity

test("worstSeverity is null when nothing failed", () => {
  expect(worstSeverity([check("a", "pass"), check("b", "skip")])).toBeNull();
});

test("worstSeverity picks the worst FAILING severity, ignoring passes and skips", () => {
  const checks = [
    check("a", "fail", { severity: "suggested" }),
    check("b", "pass", { severity: "critical" }),
    check("c", "fail", { severity: "warning" }),
  ];
  expect(worstSeverity(checks)).toBe("warning");
});

test("a failing candidate row never drives the worst severity above warning", () => {
  // secrets is "critical" in the checklist, but it is unreviewed by
  // definition — the dot must read "warning", not "critical", until a
  // session has triaged it.
  const checks = [check("secrets", "fail", { severity: "critical", kind: "candidate" })];
  expect(effectiveSeverity(checks[0])).toBe("warning");
  expect(worstSeverity(checks)).toBe("warning");
});

test("a failing FACT row at critical still reads as critical", () => {
  const checks = [check("entry", "fail", { severity: "critical", kind: "fact" })];
  expect(worstSeverity(checks)).toBe("critical");
});

test("a real critical fact outranks an unreviewed candidate", () => {
  const checks = [
    check("secrets", "fail", { severity: "critical", kind: "candidate" }),
    check("entry", "fail", { severity: "critical", kind: "fact" }),
  ];
  expect(worstSeverity(checks)).toBe("critical");
});

test("severityDotLabel names the state in words for every case", () => {
  expect(severityDotLabel(null)).toContain("not checked yet");
  expect(severityDotLabel([check("a", "pass")])).toContain("nothing to fix");
  expect(
    severityDotLabel([
      check("a", "fail", { severity: "critical" }),
      check("b", "fail", { severity: "warning" }),
    ]),
  ).toBe("App Doctor: 1 critical, 1 warning");
  // The candidate discount applies here too — the label is built off
  // effectiveSeverity, not the raw checklist severity.
  expect(
    severityDotLabel([check("secrets", "fail", { severity: "critical", kind: "candidate" })]),
  ).toBe("App Doctor: 1 warning");
});

// --------------------------------------------------------------- row wording

test("a candidate's action is Review, a fact's is Fix", () => {
  expect(rowActionLabel(check("secrets", "fail", { kind: "candidate" }))).toBe("Review");
  expect(rowActionLabel(check("readme", "fail", { kind: "fact" }))).toBe("Fix");
});

test("a failing candidate's DETAIL text reads as N to review, never as a settled failure", () => {
  const c = check("secrets", "fail", {
    kind: "candidate",
    findings: [finding("app.py", 3), finding("app.py", 9)],
  });
  expect(rowStateDetailText(c)).toBe("2 to review");
});

test("every other row's detail text falls back to STATE_LABEL", () => {
  expect(rowStateDetailText(check("readme", "pass"))).toBe("Passed");
  expect(rowStateDetailText(check("readme", "skip"))).toBe("Not checked");
  expect(rowStateDetailText(check("readme", "unrun"))).toBe("Not run yet");
});

// ---------------------------------------------------------- severity in words
//
// There is no visible severity chip in the dialog any more (owner request).
// `rowStateAccessibleLabel` is now the only place a FAILING row's severity is
// said in words at all, because it feeds the state mark's own
// `aria-label`/`title` — this is what keeps severity from vanishing out of
// the accessible tree. `rowStateDetailText` (the visible `.appdoc-detail`
// line's text) must NEVER say the severity word — an earlier pass used one
// string for both callers and put the severity word right back on screen
// inside the detail line ("Critical — Failed — <detail>"), the exact words
// removing the chip was supposed to erase.

test("a failing FACT row's accessible label names both its severity and its state", () => {
  expect(
    rowStateAccessibleLabel(check("readme", "fail", { kind: "fact", severity: "critical" })),
  ).toBe("Critical — Failed");
  expect(
    rowStateAccessibleLabel(check("readme", "fail", { kind: "fact", severity: "suggested" })),
  ).toBe("Suggested — Failed");
});

test("a failing row's accessible label names its OWN checklist severity, not the discounted effectiveSeverity", () => {
  // secrets is "critical" in the checklist even though a candidate's
  // effectiveSeverity is capped at "warning" for cross-row reductions (the
  // header dot). A row naming itself always says what the checklist actually
  // found.
  const c = check("secrets", "fail", {
    kind: "candidate",
    severity: "critical",
    findings: [finding("app.py", 3)],
  });
  expect(rowStateAccessibleLabel(c)).toBe("Critical — 1 to review");
});

test("the visible detail text never contains a severity word, only the accessible label does", () => {
  // Pins the regression: a prior pass folded severity into the one string
  // both callers used, so the word leaked from the accessible label back
  // onto the screen via the visible detail line.
  const severityWords = ["Critical", "Warning", "Suggested"];
  for (const severity of ["critical", "warning", "suggested"] as const) {
    for (const kind of ["fact", "candidate"] as const) {
      const c = check("x", "fail", {
        kind,
        severity,
        findings: kind === "candidate" ? [finding("app.py", 1)] : [],
      });
      const detail = rowStateDetailText(c);
      const accessible = rowStateAccessibleLabel(c);
      for (const word of severityWords) {
        expect(detail).not.toContain(word);
      }
      expect(accessible).toContain(SEVERITY_LABEL[severity]);
    }
  }
});
