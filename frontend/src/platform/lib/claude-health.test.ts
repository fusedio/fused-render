// What lib/claude-health gets wrong in ways no screenshot shows: which of
// several problems leads. (The dismissal half left with the unmounted strip,
// D1386.)
//
// The recurring assertion in here is the NEGATIVE one — that the strip does not
// tell a user to install a CLI they have, or to sign into an account they are
// signed into. Every one of those is the same wrong-advice failure the two-tier
// matching in trouble.ts exists to prevent, arriving from the proactive side.

import { expect, test } from "bun:test";

import {
  CLAUDE_BIN_ENV,
  CLAUDE_UPDATE_COMMAND,
  CODEX_BIN_ENV,
  COPILOT_BIN_ENV,
  GEMINI_BIN_ENV,
  agentAccountLine,
  agentIssues,
  claudeIssues,
  issueHelpUrl,
} from "./claude-health";
import { CLAUDE_INSTALL_COMMAND, CODEX_INSTALL_COMMAND, GEMINI_INSTALL_COMMAND } from "./trouble";
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
    account: { method: "claude.ai", email: "x@example.com", org: "Example", plan: "team" },
    config_dir: "/Users/x/.claude",
    platform: "darwin",
    install_command: "curl -fsSL https://claude.ai/install.sh | bash",
    broken: false,
    // A healthy machine never pays for a doctor probe, so these are what a
    // fine snapshot actually carries: no method read, and `updatable` unknown.
    install_method: null,
    updatable: null,
    update_command: "claude update",
    update_manager: null,
    update_blocked_reason: null,
    doctor: null,
    // A healthy source: "path" machine reports `true` — but `null` is the
    // never-produces-advice value,
    // so the baseline stays the quiet one and a test opts into `false`.
    on_shell_path: null,
    path_fix_command: null,
    checked_at: 1_700_000_000,
    ...over,
  };
}

const ids = (h: ClaudeHealth | null) => claudeIssues(h).map((i) => i.id);

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

// -- Codex (docs/CODEX_BACKEND.md D10) ----------------------------------------
//
// The same rules over the same keys; only the words change. Each test is the
// Codex reading of a Claude one above, plus what Codex alone says.

function codexHealthy(over: Partial<ClaudeHealth> = {}): ClaudeHealth {
  return healthy({
    path: "/Users/x/.npm-global/bin/codex",
    version: "0.163.0",
    min_version: "0.160.0",
    account: { method: "chatgpt", email: null, org: null, plan: "go" },
    install_command: "",
    update_command: null,
    ...over,
  });
}

const codexIds = (h: ClaudeHealth | null) => agentIssues(h, "codex").map((i) => i.id);
const claudeWords = () => "CLAUDE LINE";

test("codex: a healthy machine produces no issues", () => {
  expect(agentIssues(codexHealthy(), "codex")).toEqual([]);
});

test("codex: a missing CLI names Codex and offers npm, even from a server too old to say", () => {
  const [issue] = agentIssues(codexHealthy({ found: false, path: null, version: null, signed_in: false }), "codex");
  expect(issue.id).toBe("missing");
  expect(issue.title).toBe("The app can't find Codex");
  expect(issue.command).toBe(CODEX_INSTALL_COMMAND);
  expect(issue.action).toEqual({ kind: "install", label: "Install Codex" });
  // The server's own line still wins when it states one.
  const stated = agentIssues(codexHealthy({ found: false, install_command: "brew install codex" }), "codex");
  expect(stated[0].command).toBe("brew install codex");
});

test("codex: a stale override names Codex's own env var", () => {
  const [issue] = agentIssues(codexHealthy({ found: false, source: "override" }), "codex");
  expect(issue.id).toBe("unusable-override");
  expect(issue.title).toContain(CODEX_BIN_ENV);
  expect(CODEX_BIN_ENV).toBe("FUSED_RENDER_CODEX_BIN");
  expect(issue.title).not.toContain(CLAUDE_BIN_ENV);
  expect(issue.title).not.toContain(COPILOT_BIN_ENV);
});

test("codex: a broken CLI is never offered `claude doctor`", () => {
  const [issue] = agentIssues(codexHealthy({ broken: true }), "codex");
  expect(issue.id).toBe("broken");
  expect(issue.action).toBeUndefined();
  expect(issue.detail).not.toContain("diagnostics");
});

test("codex: an outdated CLI updates with npm, never `claude update`", () => {
  const [issue] = agentIssues(codexHealthy({ outdated: true, version: "0.150.0" }), "codex");
  expect(issue.id).toBe("outdated");
  expect(issue.title).toBe("Codex 0.150.0 is older than this app needs");
  expect(issue.command).toBe(CODEX_INSTALL_COMMAND);
  expect(issue.command).not.toBe(CLAUDE_UPDATE_COMMAND);
  expect(issue.action).toEqual({ kind: "update", label: "Update Codex" });
  const managed = agentIssues(codexHealthy({ outdated: true, updatable: false, update_command: "brew upgrade codex" }), "codex");
  expect(managed[0].command).toBe("brew upgrade codex");
  expect(managed[0].action).toBeUndefined();
});

test("codex: a signed-out CLI says ChatGPT and the device code, on an explicit false only", () => {
  expect(codexIds(codexHealthy({ signed_in: null }))).toEqual([]);
  const [issue] = agentIssues(codexHealthy({ signed_in: false, account: null }), "codex");
  expect(issue.id).toBe("signed-out");
  expect(issue.title).toBe("Codex isn't signed in");
  expect(issue.detail).toContain("Sign in with ChatGPT (device code)");
  expect(issue.detail).toContain("codex login");
  expect(issue.action).toEqual({ kind: "login", label: "Sign in" });
  expect(issue.helpKind).toBe("login");
});

test("codex: off the shell PATH is offered the link, naming `codex`", () => {
  const [issue] = agentIssues(codexHealthy({ on_shell_path: false, path_fix_command: "echo 'export PATH=…' >> ~/.zshrc" }), "codex");
  expect(issue.id).toBe("not-on-path");
  expect(issue.title).toBe("Codex works in this app, but not in your terminal yet");
  expect(issue.detail).toContain("`codex`");
  expect(issue.action?.kind).toBe("link-path");
  expect(issue.optional).toBe(true);
});

test("copilot is still never offered the PATH link", () => {
  expect(agentIssues(healthy({ on_shell_path: false, path_fix_command: "x" }), "copilot")).toEqual([]);
});

test("claude's words are unchanged by the per-agent table", () => {
  const [path] = claudeIssues(healthy({ on_shell_path: false, path_fix_command: "x" }));
  expect(path.title).toBe("Claude Code works in this app, but not in your terminal yet");
  expect(path.detail).toContain("so `claude` there says");
  const [old] = claudeIssues(healthy({ outdated: true, updatable: false, update_command: null }));
  expect(old.detail).toContain("Running `claude update` here would not change anything");
  const [cop] = agentIssues(healthy({ outdated: true, updatable: false, update_command: null }), "copilot");
  expect(cop.detail).toContain("Running `copilot update` here would not change anything");
  expect(cop.command).toBeUndefined();
});

test("codex's account line: ChatGPT with its plan, or an API key", () => {
  expect(agentAccountLine(codexHealthy(), "codex", claudeWords)).toBe("Signed in with ChatGPT (Go plan)");
  // `planType` is Codex's own key, for a server that passes it through.
  const planType = codexHealthy({
    account: { method: "chatgpt", email: null, org: null, plan: null, planType: "plus" },
  });
  expect(agentAccountLine(planType, "codex", claudeWords)).toBe("Signed in with ChatGPT (Plus plan)");
  const noPlan = codexHealthy({ account: { method: "chatgpt", email: null, org: null, plan: null } });
  expect(agentAccountLine(noPlan, "codex", claudeWords)).toBe("Signed in with ChatGPT");
  const key = codexHealthy({ account: { method: "api_key", email: null, org: null, plan: null } });
  expect(agentAccountLine(key, "codex", claudeWords)).toBe("Signed in with an OpenAI API key");
  expect(agentAccountLine(codexHealthy({ signed_in: false }), "codex", claudeWords)).toBeNull();
  // Claude's line is still the caller's.
  expect(agentAccountLine(healthy(), "claude", claudeWords)).toBe("CLAUDE LINE");
});


// -- Gemini (docs/GEMINI_BACKEND.md D10) ---------------------------------------

function geminiHealthy(over: Partial<ClaudeHealth> = {}): ClaudeHealth {
  return healthy({
    path: "/Users/x/.npm-global/bin/gemini",
    version: "0.65.0",
    min_version: "0.65.0",
    install_command: GEMINI_INSTALL_COMMAND,
    account: { method: "google", email: "dev@example.com", org: null, plan: null },
    ...over,
  });
}

test("gemini: a healthy machine produces no issues", () => {
  expect(agentIssues(geminiHealthy(), "gemini")).toEqual([]);
});

test("gemini: a missing CLI names Gemini CLI and offers npm, and a stale override names its env var", () => {
  const [issue] = agentIssues(geminiHealthy({ found: false, path: null, version: null, signed_in: false }), "gemini");
  expect(issue.id).toBe("missing");
  expect(issue.title).toBe("The app can't find Gemini CLI");
  expect(issue.command).toBe(GEMINI_INSTALL_COMMAND);
  expect(issue.action).toEqual({ kind: "install", label: "Install Gemini CLI" });
  const [override] = agentIssues(geminiHealthy({ found: false, source: "override" }), "gemini");
  expect(override.id).toBe("unusable-override");
  expect(override.title).toContain(GEMINI_BIN_ENV);
  expect(GEMINI_BIN_ENV).toBe("FUSED_RENDER_GEMINI_BIN");
  expect(override.title).not.toContain(CODEX_BIN_ENV);
});

test("gemini: signed out offers the Google sign-in, never `claude doctor`", () => {
  const issues = agentIssues(geminiHealthy({ signed_in: false, account: null }), "gemini");
  expect(issues.map((i) => i.id)).toEqual(["signed-out"]);
  expect(issues[0].detail).toContain("Sign in with Google");
  expect(issues[0].action?.kind).toBe("login");
  const [broken] = agentIssues(geminiHealthy({ broken: true }), "gemini");
  expect(broken.id).toBe("broken");
  expect(broken.action).toBeUndefined();
});

test("gemini: the account line names the method in Gemini's words", () => {
  const line = (over: Partial<ClaudeHealth>) => agentAccountLine(geminiHealthy(over), "gemini", () => "CLAUDE LINE");
  expect(line({})).toBe("Signed in with Google as dev@example.com");
  expect(line({ account: { method: "google", email: null, org: null, plan: null } })).toBe("Signed in with Google");
  expect(line({ account: { method: "api_key", email: null, org: null, plan: null } })).toBe("Signed in with a Gemini API key");
  expect(line({ account: { method: "vertex", email: null, org: null, plan: null } })).toBe("Signed in through Vertex AI");
  expect(line({ account: { method: "gateway", email: null, org: null, plan: null } })).toBe("Signed in through an AI API gateway");
  expect(line({ account: null })).toBe("Signed in");
  expect(line({ signed_in: false })).toBeNull();
});
