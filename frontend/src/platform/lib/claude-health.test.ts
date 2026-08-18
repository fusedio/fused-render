// What lib/claude-health gets wrong in ways no screenshot shows: which of
// several problems leads, and whether dismissing one hides the next.
//
// The recurring assertion in here is the NEGATIVE one — that the strip does not
// tell a user to install a CLI they have, or to sign into an account they are
// signed into. Every one of those is the same wrong-advice failure the two-tier
// matching in trouble.ts exists to prevent, arriving from the proactive side.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, expect, test } from "bun:test";

import {
  CLAUDE_BIN_ENV,
  CLAUDE_UPDATE_COMMAND,
  claudeIssues,
  dismiss,
  isDismissed,
  issueHelpUrl,
  issuesSignature,
  undismiss,
} from "./claude-health";
import { CLAUDE_INSTALL_COMMAND } from "./trouble";
import type { ClaudeHealth } from "./api";

// A machine where everything is fine. Each test breaks exactly one thing, so
// what it asserts is attributable to that one field.
function healthy(over: Partial<ClaudeHealth> = {}): ClaudeHealth {
  return {
    found: true,
    path: "/Users/x/.local/bin/claude",
    source: "path",
    version: "2.1.220",
    min_version: "2.0.0",
    outdated: false,
    signed_in: true,
    config_dir: "/Users/x/.claude",
    checked_at: 1_700_000_000,
    ...over,
  };
}

const ids = (h: ClaudeHealth | null) => claudeIssues(h).map((i) => i.id);

// bun's test runtime has no localStorage, and the dismissal state machine is
// half of what this file is here to check — so stand one up. Kept as a real
// (tiny) store rather than a spy: what matters is the round trip through a
// string key, which is exactly where a signature bug would hide.
const store = new Map<string, string>();
(globalThis as { localStorage?: unknown }).localStorage = {
  getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
};

beforeEach(() => {
  store.clear();
  undismiss();
});

// -- nothing to say -----------------------------------------------------------

test("a healthy machine produces no issues at all", () => {
  expect(claudeIssues(healthy())).toEqual([]);
});

test("a null snapshot is not a finding", () => {
  // A failed probe means OUR endpoint did not answer. Reporting "Claude Code is
  // missing" off the back of our own failed request would be the app blaming
  // the user's machine for its own fault.
  expect(claudeIssues(null)).toEqual([]);
});

test("an unknown sign-in state says nothing about signing in", () => {
  // null means `claude auth status` could not be asked (no runnable CLI, or one
  // predating the subcommand) — not that it answered no. There is nothing to
  // report, and reporting anyway would tell a signed-in user to go sign in.
  expect(ids(healthy({ signed_in: null }))).toEqual([]);
});

// -- the missing case short-circuits -----------------------------------------

test("a missing CLI reports only that, with the install command", () => {
  const issues = claudeIssues(healthy({
    found: false, path: null, source: null, version: null,
    // A machine with no claude is ALSO signed out and has no readable version.
    // Reporting all three would bury the only one that matters under two
    // consequences of it.
    signed_in: false, outdated: false,
  }));
  expect(issues.map((i) => i.id)).toEqual(["missing"]);
  expect(issues[0].command).toBe(CLAUDE_INSTALL_COMMAND);
  expect(issues[0].helpKind).toBe("notfound");
});

test("a stale override is its own diagnosis, never 'install Claude Code'", () => {
  // The user may well have Claude Code; what is broken is a setting they can
  // see. Telling them to install it would be wrong twice.
  const issues = claudeIssues(healthy({
    found: false, source: "override", path: "/opt/gone/claude",
  }));
  expect(issues.map((i) => i.id)).toEqual(["unusable-override"]);
  expect(issues[0].title).toContain(CLAUDE_BIN_ENV);
  expect(issues[0].command).toBeUndefined();
  // and the path is named, because it is the thing to correct
  expect(issues[0].detail).toContain("/opt/gone/claude");
});

// -- the found-but-not-ready cases -------------------------------------------

test("a shell-only install is not something to mention", () => {
  // It IS a real problem — neither spawn path shells out, so a volta/fnm/nvm
  // install is invisible to both — but one the app fixes for itself: the server
  // publishes the discovered path as the override the moment it probes
  // (claude_health.adopt). Asking the user to set an environment variable we
  // were holding the value for was asking them to do our work.
  expect(ids(healthy({ source: "shell", path: "/opt/volta/bin/claude" }))).toEqual([]);
});

test("an override that works is not something to mention", () => {
  // Someone who already set the override has solved this; saying anything would
  // be nagging about a fixed problem.
  expect(ids(healthy({ source: "override" }))).toEqual([]);
});

test("a candidate-dir install is not something to mention either", () => {
  // We found it where Claude Code installs it. Nothing for the user to do.
  expect(ids(healthy({ source: "candidate" }))).toEqual([]);
});

test("an outdated CLI offers `claude update` and names both versions", () => {
  const issues = claudeIssues(healthy({ version: "1.0.88", outdated: true }));
  expect(issues.map((i) => i.id)).toEqual(["outdated"]);
  expect(issues[0].command).toBe(CLAUDE_UPDATE_COMMAND);
  expect(issues[0].title).toContain("1.0.88");
  expect(issues[0].detail).toContain("2.0.0");
});

test("outdated is driven by the server's flag, never re-derived here", () => {
  // The server is the only side that knows an unreadable version must not count
  // as old. A UI that compared the strings itself would reintroduce exactly
  // that bug.
  expect(ids(healthy({ version: null, outdated: false }))).toEqual([]);
  expect(ids(healthy({ version: "0.1", outdated: false }))).toEqual([]);
});

test("a signed-out CLI is reported only on an explicit false", () => {
  expect(ids(healthy({ signed_in: false }))).toEqual(["signed-out"]);
  expect(ids(healthy({ signed_in: null }))).toEqual([]);
  expect(ids(healthy({ signed_in: true }))).toEqual([]);
});

test("several problems on one install are all reported, blocking first", () => {
  const issues = ids(healthy({ version: "1.0.88", outdated: true, signed_in: false }));
  expect(issues).toEqual(["outdated", "signed-out"]);
});

// -- help links ---------------------------------------------------------------

test("every issue deep-links to a real troubleshooting tab", () => {
  const cases: ClaudeHealth[] = [
    healthy({ found: false, source: null }),
    healthy({ found: false, source: "override" }),
    healthy({ outdated: true, version: "1.0.0" }),
    healthy({ signed_in: false }),
  ];
  for (const h of cases) {
    for (const issue of claudeIssues(h)) {
      expect(issueHelpUrl(issue)).toMatch(
        /^https:\/\/render\.fused\.io\/#troubleshooting-(notfound|login|limit|raw)$/,
      );
    }
  }
});

test("a signed-out install links to the login tab, not the install tab", () => {
  const [issue] = claudeIssues(healthy({ signed_in: false }));
  expect(issueHelpUrl(issue)).toContain("#troubleshooting-login");
});

// -- dismissal ---------------------------------------------------------------

test("nothing to say counts as dismissed, so the strip renders nothing", () => {
  expect(isDismissed([])).toBe(true);
});

test("dismissing hides that exact set and nothing else", () => {
  const signedOut = claudeIssues(healthy({ signed_in: false }));
  dismiss(signedOut);
  expect(isDismissed(signedOut)).toBe(true);

  // THE CASE THIS EXISTS FOR: dismissed "not signed in" a week ago, signed in
  // since, and has now upgraded into a version problem. They must hear about it.
  const outdated = claudeIssues(healthy({ outdated: true, version: "1.0.0" }));
  expect(isDismissed(outdated)).toBe(false);
});

test("a set that GREW is not dismissed", () => {
  const one = claudeIssues(healthy({ signed_in: false }));
  dismiss(one);
  const two = claudeIssues(healthy({ signed_in: false, outdated: true, version: "1.0.0" }));
  expect(isDismissed(two)).toBe(false);
});

test("the signature ignores presentation order", () => {
  // claudeIssues' order is a display decision; it must not be able to
  // invalidate a stored dismissal by changing.
  const a = claudeIssues(healthy({ signed_in: false, outdated: true, version: "1.0.0" }));
  expect(issuesSignature(a)).toBe(issuesSignature([...a].reverse()));
});

test("the signature ignores versions and paths", () => {
  // Titles carry both, so keying on them would re-show a dealt-with strip on
  // every patch upgrade.
  const first = claudeIssues(healthy({ outdated: true, version: "1.0.88" }));
  dismiss(first);
  const later = claudeIssues(healthy({ outdated: true, version: "1.0.99" }));
  expect(isDismissed(later)).toBe(true);
});

test("undismiss brings it back", () => {
  const issues = claudeIssues(healthy({ signed_in: false }));
  dismiss(issues);
  expect(isDismissed(issues)).toBe(true);
  undismiss();
  expect(isDismissed(issues)).toBe(false);
});

test("unavailable storage reads as NOT dismissed, and never throws", () => {
  // Private mode, a full quota, a locked-down origin. Showing the strip once too
  // often is a far smaller harm than silently withholding the one fact that
  // explains why nothing works — so the failure has to fall the safe way.
  const real = (globalThis as { localStorage?: unknown }).localStorage;
  (globalThis as { localStorage?: unknown }).localStorage = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
    removeItem: () => {
      throw new Error("denied");
    },
  };
  try {
    const issues = claudeIssues(healthy({ signed_in: false }));
    expect(() => dismiss(issues)).not.toThrow();
    expect(isDismissed(issues)).toBe(false);
    expect(() => undismiss()).not.toThrow();
    // ...but "nothing to say" still short-circuits without touching storage.
    expect(isDismissed([])).toBe(true);
  } finally {
    (globalThis as { localStorage?: unknown }).localStorage = real;
  }
});

// -- when the strip re-evaluates itself (ui/ClaudeHealthStrip) ---------------
//
// Behaviour of the component rather than this lib, pinned the way the shell's
// own suites pin theirs — over the source, because the regressions here are
// "the effect stopped running" and "a flag shadowed the check", neither of
// which a render assertion would notice.

const STRIP = readFileSync(
  join(import.meta.dir, "..", "ui", "ClaudeHealthStrip.tsx"), "utf8");

test("mounting always re-asks; the module cache only seeds the first paint", () => {
  // The regression: `if (cached !== null) return` skipped the fetch, so the
  // only thing that ever refreshed the strip was its own button. A user who
  // signed in and navigated back still faced a card telling them to sign in.
  expect(STRIP).not.toContain("if (cached !== null) return");
  expect(STRIP).toContain("useEffect(() => {\n    load(false);\n  }, [load]);");
});

test("returning to the window re-checks, and only while something is showing", () => {
  // Every fix this card asks for happens elsewhere — a terminal, an installer —
  // so coming back is exactly when the claim should be re-tested and the card
  // allowed to disappear on its own.
  expect(STRIP).toContain('window.addEventListener("focus", onFocus)');
  expect(STRIP).toContain('document.addEventListener("visibilitychange", onFocus)');
  expect(STRIP).toContain("if (!showing) return;");
  // Forced, because the server cache is age-bounded and a sign-in usually lands
  // inside that window — a plain read is the one that would still say "signed out".
  expect(STRIP).toContain("load(true);");
  // Both listeners are removed, or navigating away leaves probes firing forever.
  expect(STRIP).toContain('window.removeEventListener("focus", onFocus)');
  expect(STRIP).toContain('document.removeEventListener("visibilitychange", onFocus)');
});

test("focus bursts collapse into one check", () => {
  expect(STRIP).toContain("Date.now() - lastCheck.current < FOCUS_RECHECK_MS");
});

test("a hidden document does not count as coming back", () => {
  expect(STRIP).toContain('document.visibilityState === "hidden"');
});

test("dismissal is decided only by the signature check, never a local flag", () => {
  // A `closed` flag shadowed isDismissed and was wrong in the direction that
  // matters: dismissing "not signed in" suppressed a LATER, different problem
  // for the rest of the page's life.
  expect(STRIP).not.toContain("setClosed");
  expect(STRIP).toContain("!isDismissed(issues)");
});
