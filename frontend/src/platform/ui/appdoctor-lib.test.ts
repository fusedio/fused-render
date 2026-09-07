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
  findingWhere,
  MAX_FINDINGS_SHOWN,
  splitFindings,
  STATE_LABEL,
  summaryLine,
  tasksTabUrl,
} = lib;

const check = (id: string, state: AppCheck["state"]): AppCheck => ({
  id,
  label: id,
  state,
  detail: "",
  findings: [],
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

test("a failing report counts the failures against every row", () => {
  const checks = [check("a", "fail"), check("b", "pass"), check("c", "fail")];
  expect(summaryLine(checks)).toBe("2 of 3 checks failed.");
});

test("failures and skips are counted separately", () => {
  const checks = [check("a", "fail"), check("b", "skip"), check("c", "skip")];
  expect(summaryLine(checks)).toBe("1 of 3 checks failed. 2 could not be checked.");
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
  expect(Object.keys(STATE_LABEL).sort()).toEqual(["fail", "pass", "skip"]);
  expect(Object.values(STATE_LABEL).every((v) => v.length > 0)).toBe(true);
});
