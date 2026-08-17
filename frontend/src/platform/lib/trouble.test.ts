// The classification is the whole feature: get it wrong and the user is sent
// to a page about installing something they already have.
import { expect, test } from "bun:test";

import {
  CLAUDE_INSTALL_COMMAND,
  resetAnnouncedTemplateErrors,
  shouldAnnounceTemplateError,
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
      "could not write the incident file: [Errno 2] No such file or directory: " +
        "'/Volumes/gone/.fused-render-selffix/incidents/x.md'"
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
    page: "/preferences?tab=selffix",
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
