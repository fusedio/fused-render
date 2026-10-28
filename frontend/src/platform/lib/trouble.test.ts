// The classification is the whole feature: get it wrong and the user is sent
// to a page about installing something they already have.
import { expect, test } from "bun:test";

import {
  CLAUDE_INSTALL_COMMAND,
  CLAUDE_INSTALL_COMMAND_WINDOWS,
  CODEX_INSTALL_COMMAND,
  GEMINI_INSTALL_COMMAND,
  troubleInstructions,
  resetAnnouncedTemplateErrors,
  shouldAnnounceTemplateError,
  syncRegistryToast,
  isClaudeTrouble,
  troubleHelpUrl,
  troubleKind,
  troubleReport,
} from "./trouble";

test("the server's own not-found message is recognised", () => {
  // The exact string claude_spawn.py returns when the CLI is missing.
  expect(
    troubleKind(
      "Claude Code isn't installed (or couldn't be found). Install it, check " +
        "that `claude` runs in a terminal, then try again."
    )
  ).toBe("notfound");
  // ...and the agent's own, which is what a raw spawn failure carries up.
  expect(troubleKind("claude CLI not found — install Claude Code")).toBe("notfound");
});

test("a signed-out claude is NOT reported as a missing one", () => {
  // It is installed and it ran; telling this user to install it wastes the one
  // minute they needed. The download page separates these for the same reason.
  // The CLI's own text names itself, so it needs no help.
  expect(troubleKind("Invalid API key · Please run /login")).toBe("login");
  expect(troubleKind("Claude Code is not signed in")).toBe("login");
});

test("a sign-in problem with no subject is not assumed to be Claude's", () => {
  // This app has several sign-ins — the Fused account, a cloud deploy, an S3
  // mount — and "not signed in" on its own belongs to none of them in
  // particular. Guessing Claude here would send someone to a terminal to run
  // `/login` about their bucket credentials.
  expect(troubleKind("You are not signed in")).toBe("raw");
  expect(troubleKind("oauth token expired")).toBe("raw");
});

test("a usage limit is its own case, because nothing is broken", () => {
  expect(troubleKind("Usage limit reached — resets at 4pm")).toBe("limit");
  expect(troubleKind("session limit exceeded")).toBe("limit");
});

test("a failure SHAPE about something else is not about Claude", () => {
  // The one that mattered, and the one my own test dodged by picking Errno 28:
  // this app produces ENOENT about files, mounts and credentials constantly,
  // and answering a disk-path problem with "install Claude Code" is the same
  // wrong advice TR-2 exists to prevent, arriving from the other side.
  expect(
    troubleKind(
      "could not write the report: [Errno 2] No such file or directory: " +
        "'/Volumes/gone/notes/report.md'"
    )
  ).toBe("raw");
  expect(troubleKind("mount probe failed: authentication failed for s3://bucket")).toBe("raw");
  expect(troubleKind("ffmpeg: command not found")).toBe("raw");
  expect(troubleKind("rate limit exceeded talking to the tile server")).toBe("raw");
});

test("...but the same shape IS about Claude when the message says so", () => {
  // The subject is what promotes a shape, and it is usually right there in the
  // path the OS could not find.
  expect(troubleKind("[Errno 2] No such file or directory: 'claude'")).toBe("notfound");
  expect(troubleKind("spawning claude: command not found")).toBe("notfound");
});

test("anything else is `raw`, which is a real answer and not a shrug", () => {
  expect(troubleKind("could not write the incident file: [Errno 28]")).toBe("raw");
  expect(troubleKind("")).toBe("raw");
  expect(isClaudeTrouble("raw")).toBe(false);
  expect(isClaudeTrouble("notfound")).toBe(true);
});

test("the more specific case wins over the broader one", () => {
  // "not found" appears inside plenty of messages whose real cause is named
  // earlier in the same sentence; the ordering is what keeps those correct.
  expect(troubleKind("Invalid API key — credentials file not found")).toBe("login");
});

test("the help link deep-links to the matching tab", () => {
  // The page reads `#troubleshooting-<kind>` and selects that tab, so this
  // lands on the answer rather than on a long page about everything.
  expect(troubleHelpUrl("notfound")).toBe(
    "https://render.fused.io/#troubleshooting-notfound"
  );
  expect(troubleHelpUrl("raw")).toBe("https://render.fused.io/#troubleshooting-raw");
});

test("the report carries the installation, which is what the user cannot look up", () => {
  const text = troubleReport({
    what: "starting a fix session on this installation",
    error: "Claude Code isn't installed (or couldn't be found).",
    install_root: "/Applications/FusedRender.app/Contents/Resources/lib/python3.12/fused_render",
    version: "0.4.22",
    platform: "macOS-15.0",
    page: "/explorer/view/Users/me/data.parquet",
  });
  expect(text).toContain("/Applications/FusedRender.app");
  expect(text).toContain("v0.4.22");
  expect(text).toContain("macOS-15.0");
  expect(text).toContain("Claude Code isn't installed");
  // ...and the link matches the error it carries, not a generic one.
  expect(text).toContain("#troubleshooting-notfound");
  // What was happening comes before what went wrong: a paste that opens with a
  // traceback makes the reader work out the question first.
  expect(text.indexOf("What the app was doing")).toBeLessThan(text.indexOf("Error:"));
});

test("a report with nothing but an error is still worth pasting", () => {
  // The boot failure has no config to describe itself with — that is the thing
  // that failed — so the block must degrade to the error and the help link
  // rather than printing a row of empty labels.
  const text = troubleReport({ what: "loading the app's configuration", error: "HTTP 500" });
  expect(text).toContain("HTTP 500");
  expect(text).toContain("#troubleshooting-raw");
  expect(text).not.toContain("Installation:");
  expect(text).not.toContain("undefined");
});

test("the install command is the one the download page tells people to run", () => {
  // Pinned because it is shown as a thing to copy and paste into a terminal:
  // a wrong command here is worse than no command.
  expect(CLAUDE_INSTALL_COMMAND).toBe("curl -fsSL https://claude.ai/install.sh | bash");
  expect(CLAUDE_INSTALL_COMMAND_WINDOWS).toBe("irm https://claude.ai/install.ps1 | iex");
});


test("a broken registry is announced once, not once per file", () => {
  // The error rides EVERY stat, so the naive wiring is a toast on every click
  // for as long as the registry stays broken.
  resetAnnouncedTemplateErrors();
  const err = "cannot read registry.json: Expecting property name (char 2)";
  expect(shouldAnnounceTemplateError(err)).toBe(true);
  expect(shouldAnnounceTemplateError(err)).toBe(false);
  expect(shouldAnnounceTemplateError(err)).toBe(false);
});

test("a DIFFERENT kind of broken is said again", () => {
  // The user edited the registry and got it wrong another way. That is new
  // news, and staying quiet would look like the first fix worked.
  resetAnnouncedTemplateErrors();
  expect(shouldAnnounceTemplateError("cannot read registry.json: bad JSON")).toBe(true);
  expect(shouldAnnounceTemplateError("registry.json: not an object")).toBe(true);
});

test("an empty message is never announced", () => {
  resetAnnouncedTemplateErrors();
  expect(shouldAnnounceTemplateError("")).toBe(false);
  expect(shouldAnnounceTemplateError("   ")).toBe(false);
});


// -- the brief for an agent ---------------------------------------------------

test("the instructions tell an agent what to RUN, not just what broke", () => {
  // A prompt that only pastes an error gets a guess back. The first command is
  // per-case for the same reason: there is no point checking PATH for a CLI
  // that is installed and merely signed out.
  const missing = troubleInstructions({
    what: "starting a fix session",
    error: "claude CLI not found",
    install_root: "/opt/fused_render",
    version: "0.4.23",
  });
  expect(missing).toContain("which claude");
  expect(missing).toContain("curl -fsSL https://claude.ai/install.sh | bash");
  expect(missing).toContain("/opt/fused_render");

  const signedOut = troubleInstructions({
    what: "starting a fix session",
    error: "Invalid API key · Please run /login",
  });
  expect(signedOut).toContain("/login");
  // ...and must NOT tell someone to install what they already have.
  expect(signedOut).not.toContain("install.sh");
});

test("a usage limit tells the agent to change nothing", () => {
  // The one case where the correct action is no action, and an agent left to
  // its own devices will happily "fix" a working configuration.
  const text = troubleInstructions({ what: "x", error: "Usage limit reached" });
  expect(text).toContain("nothing is broken");
  expect(text).not.toContain("install.sh");
});

test("the instructions and the report are different documents", () => {
  // Same failure, two readers: one describes it to a person, the other briefs
  // an agent. If they ever converge, one of the two buttons is dead weight.
  const ctx = { what: "starting a fix session", error: "claude CLI not found" };
  expect(troubleInstructions(ctx)).not.toBe(troubleReport(ctx));
  expect(troubleReport(ctx)).not.toContain("Please:");
  expect(troubleInstructions(ctx)).toContain("Please:");
});

test("with no install path the brief says how to FIND the app", () => {
  // The boot failure is the case that cannot describe itself — `/api/config` is
  // what failed, so there is no version, no platform and no install path. A
  // brief that names no directory sends an agent looking for an app it has no
  // way to locate.
  const text = troubleInstructions({
    what: "loading the app's configuration at startup (GET /api/config)",
    error: "Failed to fetch",
  });
  // /Applications first: the DMG is how people actually have this, and the
  // second line is the same bundle one level in — the Python a fix edits.
  expect(text).toContain("/Applications/FusedRender.app");
  expect(text.indexOf("/Applications/FusedRender.app")).toBeLessThan(
    text.indexOf("brew list --cask")
  );
  // brew answers a different question: not where, but who manages it.
  expect(text).toContain("brew list --cask fused-render");
  // Neither of the probes that name an unsupported install method: a bare
  // python3 is not the bundle's interpreter, and an agent handed its answer
  // edits a copy the app does not run.
  expect(text).not.toContain("pip show");
  expect(text).not.toContain("import fused_render");
  // ...and it must not invent one, or print an empty label where one goes.
  expect(text).not.toContain("undefined");
  expect(text).not.toContain("Fused Render is installed at:");
});

test("with an install path the brief states it instead of hunting for it", () => {
  // Knowing the answer and still printing three commands to find it is how a
  // brief teaches an agent to distrust what it was told.
  const text = troubleInstructions({
    what: "starting a fix session",
    error: "claude CLI not found",
    install_root: "/opt/fused_render",
  });
  expect(text).toContain("The installed app: /opt/fused_render");
  expect(text).not.toContain("brew list --cask");
});

test("the brief separates the installation from the user's own data", () => {
  // A reinstall replaces one and never touches the other, and ~/.fused-render
  // is where the template registry lives — itself one of the four failures
  // here. An agent that conflates them fixes the wrong directory.
  const text = troubleInstructions({
    what: "reading the template registry",
    error: "registry.json: Expecting property name",
    install_root: "/opt/fused_render",
  });
  expect(text).toContain("~/.fused-render");
  expect(text).toContain("a reinstall does not touch it");
  // The app's own log, which is per-pid in the log home and therefore a glob.
  expect(text).toContain("fused-render-*.log");
});

test("the unknown-path line states no cause, because there are several", () => {
  // It first read "the failure above is what would have told me", which is true
  // of the boot failure and of nothing else: the preview fallback and the
  // builder's hero have no path for a duller reason (the snapshot that carries
  // one costs a brew shell-out), and the chat template never knows. An agent
  // that catches the app deducing wrong about its own state has reason to
  // discount the rest of the brief, so the line says only what holds
  // everywhere — and the same words in every surface that lacks a path.
  const boot = troubleInstructions({
    what: "loading the app's configuration at startup (GET /api/config)",
    error: "Failed to fetch",
  });
  const missing = troubleInstructions({
    what: "starting a fix session",
    error: "claude CLI not found",
  });
  for (const text of [boot, missing]) {
    expect(text).toContain("I do not know where the app is installed");
    expect(text).not.toContain("the failure above");
    expect(text).not.toContain("would have told me");
  }
});

test("a registry that is fixed and breaks again is announced again", () => {
  // The recovery path Preview relies on. The sticky toast is taken down when
  // `template_error` clears, and the message is forgotten at the same moment —
  // otherwise the identical fault arriving again (a repair that did not hold, a
  // second bad edit) would stay silent, which reads as the fix having worked.
  resetAnnouncedTemplateErrors();
  const err = "cannot read registry.json: Expecting property name (char 2)";
  expect(shouldAnnounceTemplateError(err)).toBe(true);
  expect(shouldAnnounceTemplateError(err)).toBe(false);
  resetAnnouncedTemplateErrors(); // what the recovery does
  expect(shouldAnnounceTemplateError(err)).toBe(true);
});


// -- the sticky registry toast's lifecycle ------------------------------------
//
// A state machine with three transitions, and two of them have already been
// wrong once: an ungated push, and an id overwritten so the first toast could
// never be taken down. Both were invisible in a component effect.

function recorder() {
  const pushed: number[] = [];
  const dismissed: number[] = [];
  let next = 1;
  return {
    pushed,
    dismissed,
    io: {
      push: () => {
        const id = next++;
        pushed.push(id);
        return id;
      },
      dismiss: (id: number) => dismissed.push(id),
    },
  };
}

test("a broken registry is announced once, however many files are opened", () => {
  resetAnnouncedTemplateErrors();
  const r = recorder();
  const err = "cannot read registry.json: Expecting property name (char 2)";
  syncRegistryToast(err, true, r.io);
  syncRegistryToast(err, true, r.io);
  syncRegistryToast(err, true, r.io);
  expect(r.pushed).toEqual([1]);
  expect(r.dismissed).toEqual([]);
});

test("a file with a better answer beside it says nothing, and takes nothing down", () => {
  // The no-view file has RegistryFixNotice and its repair button. Announcing
  // there would describe one fault twice on one screen — but walking onto that
  // file must not dismiss a claim that is still perfectly true either.
  resetAnnouncedTemplateErrors();
  const r = recorder();
  syncRegistryToast("bad json", true, r.io);
  syncRegistryToast("bad json", false, r.io);
  expect(r.pushed).toEqual([1]);
  expect(r.dismissed).toEqual([]);
});

test("a DIFFERENT kind of broken supersedes the toast it replaces", () => {
  // The registry has one state at a time, so the old message is stale by
  // definition. Pushing without dismissing left two sticky toasts and only ever
  // took down the newer — the older then outlived a successful repair.
  resetAnnouncedTemplateErrors();
  const r = recorder();
  syncRegistryToast("registry.json: bad json", true, r.io);
  syncRegistryToast("registry.json: not an object", true, r.io);
  expect(r.pushed).toEqual([1, 2]);
  expect(r.dismissed).toEqual([1]);
});

test("a registry that parses again takes the claim down", () => {
  resetAnnouncedTemplateErrors();
  const r = recorder();
  syncRegistryToast("bad json", true, r.io);
  syncRegistryToast("", true, r.io);
  expect(r.dismissed).toEqual([1]);
  // ...and a second clean stat does not dismiss anything a second time.
  syncRegistryToast("", true, r.io);
  expect(r.dismissed).toEqual([1]);
});

test("the same fault after a repair is announced again", () => {
  resetAnnouncedTemplateErrors();
  const r = recorder();
  const err = "cannot read registry.json: bad json";
  syncRegistryToast(err, true, r.io);
  syncRegistryToast("", true, r.io);
  syncRegistryToast(err, true, r.io);
  expect(r.pushed).toEqual([1, 2]);
  // Nothing stale to take down the second time: the first was already gone.
  expect(r.dismissed).toEqual([1]);
});

test("an empty or blank error is never announced", () => {
  resetAnnouncedTemplateErrors();
  const r = recorder();
  syncRegistryToast("", true, r.io);
  syncRegistryToast("   ", true, r.io);
  expect(r.pushed).toEqual([]);
  expect(r.dismissed).toEqual([]);
});

// -- Codex (docs/CODEX_BACKEND.md D13, §8) ------------------------------------

test("codex's own markers classify a Codex session's failure", () => {
  // The account markers, verbatim from agent_backends/codex.py.
  for (const msg of [
    "Not logged in",
    '{"account":null,"requiresOpenaiAuth":true}',
    "Run `codex login` first",
    "401 Unauthorized",
    "Authentication failed",
    "login required",
    "Please sign in again",
  ]) expect(troubleKind(msg, "codex")).toBe("login");
  // The limit markers, likewise.
  for (const msg of [
    "Rate limit reached for gpt-6-luna",
    "You've hit your usage limit. Try again at 3pm.",
    "quota exceeded",
    "insufficient_quota",
    "spend control limit reached",
    "Too Many Requests",
    "HTTP 429",
  ]) expect(troubleKind(msg, "codex")).toBe("limit");
});

test("codex's markers are Codex's alone", () => {
  // The same sentence on a Claude session is not about an OpenAI account.
  expect(troubleKind("401 Unauthorized", "claude")).toBe("raw");
  expect(troubleKind("spend control limit reached", "copilot")).toBe("raw");
  // A 429 that is only part of a bigger number is not a limit.
  expect(troubleKind("listening on port 14290", "codex")).toBe("raw");
});

test("a failure shape counts for Codex when it names Codex or OpenAI", () => {
  expect(troubleKind("codex: command not found", "codex")).toBe("notfound");
  expect(troubleKind("spawn codex ENOENT", "codex")).toBe("notfound");
  // ...but a disk path is still not about Codex.
  expect(troubleKind("could not write the file: no such file or directory", "codex")).toBe("raw");
  // And a Claude-shaped message on a Codex session is not Codex's.
  expect(troubleKind("claude: command not found", "codex")).toBe("raw");
});

test("the Codex brief installs with npm and signs in with a device code", () => {
  const missing = troubleInstructions({ what: "x", error: "codex: command not found", backend: "codex" });
  expect(missing).toContain("which codex");
  expect(missing).toContain(CODEX_INSTALL_COMMAND);
  expect(missing).not.toContain("install.sh");
  expect(missing).toContain("codex --version");

  const signedOut = troubleInstructions({ what: "x", error: "Not logged in", backend: "codex" });
  expect(signedOut).toContain("codex login --device-auth");
  expect(signedOut).not.toContain("/login");
  expect(signedOut).not.toContain(CODEX_INSTALL_COMMAND);
});


// -- Gemini (docs/GEMINI_BACKEND.md D13, §8) -----------------------------------

test("gemini's own markers classify a Gemini session's failure", () => {
  // The account markers, verbatim from agent_backends/gemini.py.
  for (const msg of [
    "Gemini API key is missing or not configured.",
    "Authentication required.",
    "Not logged in",
    "gemini is not signed in: the CLI asked for a browser sign-in",
    "UNAUTHENTICATED: Request had invalid authentication credentials",
    "invalid_grant: Token has been expired or revoked.",
    "login required",
    "Please sign in again",
    "HTTP 401 Unauthorized",
  ]) expect(troubleKind(msg, "gemini")).toBe("login");
  // Not login failures (review on #1939): Google's PERMISSION_DENIED is an
  // API-not-enabled / IAM refusal a signed-in user gets, a bare 401 is a
  // number, "design in" is not "sign in".
  for (const msg of [
    "PERMISSION_DENIED: Generative Language API has not been used in project 1234 before or it is disabled",
    "Read 401 bytes from stdout",
    "Could not apply the design in index.html",
    "Could not assign input",
  ]) expect(troubleKind(msg, "gemini")).toBe("raw");
  // The limit markers, likewise.
  for (const msg of [
    "RESOURCE_EXHAUSTED: quota exceeded",
    "Rate limit exceeded",
    "You exceeded your current quota",
    "Too Many Requests",
    "HTTP 429",
  ]) expect(troubleKind(msg, "gemini")).toBe("limit");
});

test("gemini's markers are Gemini's alone, and a shape counts when it names Gemini or Google", () => {
  expect(troubleKind("RESOURCE_EXHAUSTED", "claude")).toBe("raw");
  expect(troubleKind("invalid_grant", "codex")).toBe("raw");
  expect(troubleKind("listening on port 14290", "gemini")).toBe("raw");
  expect(troubleKind("build 4011 done", "gemini")).toBe("raw");
  expect(troubleKind("gemini: command not found", "gemini")).toBe("notfound");
  expect(troubleKind("spawn gemini ENOENT", "gemini")).toBe("notfound");
  expect(troubleKind("could not write the file: no such file or directory", "gemini")).toBe("raw");
  expect(troubleKind("claude: command not found", "gemini")).toBe("raw");
  // A missing file INSIDE the CLI's own config dir is an installed CLI's
  // problem, not a missing CLI — for every agent.
  expect(troubleKind("ENOENT: no such file or directory, open '/Users/me/.gemini/settings.json'", "gemini")).toBe("raw");
  expect(troubleKind("ENOENT: no such file or directory, open 'C:\\Users\\me\\.gemini\\trusted_folders.json'", "gemini")).toBe("raw");
  expect(troubleKind("ENOENT: open '/Users/me/.codex/config.toml'", "codex")).toBe("raw");
  expect(troubleKind("claude: ENOENT /Users/me/.claude/settings.json", "claude")).toBe("raw");
  // ...while the binary itself missing still reads as notfound — also when
  // the binary sits under the config dir (Claude's legacy `~/.claude/local`).
  expect(troubleKind("/usr/local/bin/gemini: No such file or directory", "gemini")).toBe("notfound");
  expect(troubleKind("spawn /Users/me/.claude/local/claude ENOENT", "claude")).toBe("notfound");
  expect(troubleKind("[Errno 2] No such file or directory: '/Users/me/.claude/local/claude'", "claude")).toBe("notfound");
  expect(troubleKind("ENOENT: no such file or directory, scandir '/Users/me/.gemini'", "gemini")).toBe("raw");
  // The server's own "not found" sentence for each agent (agent.py) is a
  // missing install, not an engine fault.
  expect(troubleKind("session start failed: gemini CLI not found — install with `npm install -g @google/gemini-cli`, put `gemini` on the PATH", "gemini")).toBe("notfound");
  expect(troubleKind("codex CLI not found — install with `npm install -g @openai/codex`", "codex")).toBe("notfound");
  expect(troubleKind("copilot CLI not found — install GitHub Copilot CLI", "copilot")).toBe("notfound");
});

test("the Gemini brief installs with npm and signs in with Google", () => {
  const missing = troubleInstructions({ what: "x", error: "gemini: command not found", backend: "gemini" });
  expect(missing).toContain("which gemini");
  expect(missing).toContain(GEMINI_INSTALL_COMMAND);
  expect(missing).not.toContain("install.sh");
  expect(missing).toContain("gemini --version");
  const signedOut = troubleInstructions({ what: "x", error: "Not logged in", backend: "gemini" });
  expect(signedOut).toContain("/auth");
  expect(signedOut).toContain("GEMINI_API_KEY");
  expect(signedOut).not.toContain("codex login");
  expect(signedOut).not.toContain(GEMINI_INSTALL_COMMAND);
});
