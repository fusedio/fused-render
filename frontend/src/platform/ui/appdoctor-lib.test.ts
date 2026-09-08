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
  failingCount,
  findingWhere,
  groupBySection,
  MAX_FINDINGS_SHOWN,
  readinessCount,
  readinessSentence,
  reviewNote,
  rowActionLabel,
  rowStateAccessibleLabel,
  rowStateDetailText,
  rowVisibleDetailText,
  severityDotLabel,
  SEVERITY_LABEL,
  sortByAttention,
  splitFindings,
  STATE_LABEL,
  stripLabel,
  tasksTabUrl,
  tickTone,
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

test("the strip's reading counts what wants an answer, and says so only when nothing does", () => {
  const clean = [check("a", "pass"), check("b", "pass")];
  expect(readinessCount(clean)).toBe("All 2");
  expect(readinessSentence(clean)).toBe("checks passed. This app is ready to share.");

  // The whole point of the wording: two passes and a skip is NOT "all clear",
  // and the count must not read "All 3" over a row nothing answered.
  const skipped = [check("a", "pass"), check("b", "pass"), check("c", "skip")];
  expect(readinessCount(skipped)).toBe("2 of 3");
  expect(readinessSentence(skipped)).toBe(
    "checks passed and the rest could not be answered here — nothing to fix.",
  );

  const failing = [
    check("a", "fail", { severity: "critical" }),
    check("b", "fail", { severity: "warning" }),
    check("c", "pass"),
    check("d", "unrun"),
  ];
  expect(readinessCount(failing)).toBe("2 of 4");
  expect(readinessSentence(failing)).toBe(
    "checks need attention before this app is worth sharing, and 1 could not be answered here.",
  );
  expect(failingCount(failing)).toBe(2);
});

test("a tick takes its row's severity when failing, and is idle when nothing answered it", () => {
  expect(tickTone(check("a", "fail", { severity: "critical" }))).toBe("critical");
  expect(tickTone(check("b", "fail", { severity: "warning" }))).toBe("warning");
  expect(tickTone(check("c", "pass"))).toBe("pass");
  expect(tickTone(check("d", "skip"))).toBe("idle");
  expect(tickTone(check("e", "unrun"))).toBe("idle");
  // A candidate's severity is its own until reviewed — `effectiveSeverity`
  // decides, and the tick follows it rather than the raw field.
  expect(
    tickTone(
      check("secrets", "fail", {
        severity: "critical",
        kind: "candidate",
        findings: [finding("a.py", 1)],
      }),
    ),
  ).toBe(effectiveSeverity(
    check("secrets", "fail", {
      severity: "critical",
      kind: "candidate",
      findings: [finding("a.py", 1)],
    }),
  ));
});

test("the strip's screen-reader label carries the same counts the ticks encode, worst first, with no zero part", () => {
  const checks = [
    check("a", "fail", { severity: "warning" }),
    check("b", "fail", { severity: "warning" }),
    check("c", "fail", { severity: "critical" }),
    check("d", "pass"),
    check("e", "skip"),
  ];
  expect(stripLabel(checks)).toBe("5 checks: 1 critical, 2 warnings, 1 passed, 1 not checked");
  expect(stripLabel([check("a", "pass")])).toBe("1 checks: 1 passed");
});

test("the footer's note names the candidate rows its task will read rather than rewrite, and says nothing when there are none", () => {
  expect(reviewNote([check("a", "fail", { severity: "warning" })])).toBe("");
  expect(
    reviewNote([
      check("secrets", "fail", { severity: "critical", kind: "candidate" }),
      check("a", "fail", { severity: "warning" }),
    ]),
  ).toBe("1 of these is a match to read, not a fix.");
  expect(
    reviewNote([
      check("secrets", "fail", { severity: "critical", kind: "candidate" }),
      check("device-paths", "fail", { severity: "warning", kind: "candidate" }),
    ]),
  ).toBe("2 of these are matches to read, not fixes.");
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

// -------------------------------------------------------------- attention order

test("sortByAttention orders fail, unrun, skip, pass, and returns a NEW array", () => {
  const checks = [
    check("a", "pass"),
    check("b", "skip"),
    check("c", "fail", { severity: "warning" }),
    check("d", "unrun"),
  ];
  const sorted = sortByAttention(checks);
  expect(sorted.map((c) => c.id)).toEqual(["c", "d", "b", "a"]);
  expect(sorted).not.toBe(checks);
  // The input is untouched.
  expect(checks.map((c) => c.id)).toEqual(["a", "b", "c", "d"]);
});

test("sortByAttention ranks a failing row's severity via effectiveSeverity, so a candidate's discount applies here too", () => {
  const checks = [
    check("warn", "fail", { severity: "warning", kind: "fact" }),
    check("secrets", "fail", { severity: "critical", kind: "candidate" }),
    check("entry", "fail", { severity: "critical", kind: "fact" }),
  ];
  const sorted = sortByAttention(checks);
  // "entry" is a real critical fact and sorts first; "secrets" is a critical
  // candidate whose effectiveSeverity is discounted to warning, so it ties
  // with "warn" and keeps its original relative order (stability) rather
  // than jumping ahead of it.
  expect(sorted.map((c) => c.id)).toEqual(["entry", "warn", "secrets"]);
});

test("sortByAttention is stable within a tier — ties keep the server's own order", () => {
  const checks = [
    check("a", "fail", { severity: "critical" }),
    check("b", "fail", { severity: "critical" }),
    check("c", "fail", { severity: "critical" }),
    check("d", "pass"),
    check("e", "pass"),
  ];
  expect(sortByAttention(checks).map((c) => c.id)).toEqual(["a", "b", "c", "d", "e"]);
});

// ------------------------------------------------------------------ severity

test("worstSeverity is null when nothing failed", () => {
  expect(worstSeverity([check("a", "pass"), check("b", "skip")])).toBeNull();
});

test("worstSeverity picks the worst FAILING severity, ignoring passes and skips", () => {
  const checks = [
    check("a", "fail", { severity: "warning" }),
    check("b", "pass", { severity: "critical" }),
    check("c", "fail", { severity: "critical" }),
  ];
  expect(worstSeverity(checks)).toBe("critical");
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

test("severityDotLabel pluralizes the severity noun when more than one row fails at that severity", () => {
  expect(
    severityDotLabel([
      check("a", "fail", { severity: "warning" }),
      check("b", "fail", { severity: "warning" }),
    ]),
  ).toBe("App Doctor: 2 warnings");
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

// §16: the visible line no longer prints the bare "Failed" prefix — the left
// rail, ground tint and state icon shape already say a row failed three
// other ways, so repeating the word in the sentence was pure noise. The
// candidate's "N to review" count survives because it is real information
// (how many findings are unreviewed), not a restatement of the state.

test("a failing FACT row's visible text is just the detail — no Failed prefix", () => {
  const c = check("fused-api-version", "fail", {
    kind: "fact",
    detail: "declares version 0; the runtime is on 1",
  });
  expect(rowVisibleDetailText(c)).toBe("declares version 0; the runtime is on 1");
  expect(rowVisibleDetailText(c)).not.toContain("Failed");
});

test("a failing CANDIDATE row's visible text keeps the N to review count", () => {
  const c = check("secrets", "fail", {
    kind: "candidate",
    detail: "looks like an API key or token",
    findings: [finding("app.py", 3), finding("app.py", 9)],
  });
  expect(rowVisibleDetailText(c)).toBe("2 to review — looks like an API key or token");
});

test("a skipped/unrun row's visible text is just the detail", () => {
  for (const state of ["skip", "unrun"] as const) {
    const c = check("readme", state, { detail: `${state} detail sentence` });
    expect(rowVisibleDetailText(c)).toBe(c.detail);
  }
});

test("a passing row draws no visible detail sentence, even when the check carries one", () => {
  // The checklist label is already a complete statement for a pass
  // ("pyproject.toml is valid TOML"), so the detail line is dropped rather
  // than echoed under it — AppDoctorModal.tsx skips `.appdoc-detail` entirely
  // when this returns "".
  const c = check("readme", "pass", { detail: "README.md exists" });
  expect(rowVisibleDetailText(c)).toBe("");
});

// ---------------------------------------------------------- severity in words
//
// There is no visible severity chip in the dialog. `rowStateAccessibleLabel`
// is the only place a FAILING row's severity is said in words at all,
// because it feeds the state mark's own `aria-label`/`title` — this is what
// keeps severity from vanishing out of the accessible tree.
// `rowStateDetailText` (the input to `rowVisibleDetailText`, which builds the
// visible `.appdoc-detail` line's text) must NEVER say the severity word —
// folding it into the same string both callers use would put the severity
// word right back on screen inside the detail line ("Critical — Failed —
// <detail>").

test("a failing FACT row's accessible label names both its severity and its state", () => {
  expect(
    rowStateAccessibleLabel(check("readme", "fail", { kind: "fact", severity: "critical" })),
  ).toBe("Critical — Failed");
  expect(
    rowStateAccessibleLabel(check("readme", "fail", { kind: "fact", severity: "warning" })),
  ).toBe("Warning — Failed");
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
  const severityWords = ["Critical", "Warning"];
  for (const severity of ["critical", "warning"] as const) {
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
