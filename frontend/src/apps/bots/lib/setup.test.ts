import { describe, expect, test } from "bun:test";
import { chromeInstallPlan, endsOnNoBrowser, setupIssues } from "./setup";

describe("setupIssues", () => {
  test("nothing while the check is unknown", () => {
    expect(setupIssues(null)).toEqual([]);
    expect(setupIssues({ chrome: { found: null, path: null }, claude: { found: null } })).toEqual([]);
  });
  test("all good: no lines", () => {
    expect(setupIssues({ chrome: { found: true, path: "/Applications/Google Chrome.app" }, claude: { found: true, signed_in: true } })).toEqual([]);
  });
  test("missing Chrome, missing Claude Code", () => {
    expect(setupIssues({ chrome: { found: false, path: null }, claude: { found: false } })).toEqual([
      "Chrome not found: bots drive Google Chrome, so install it first.",
      "Claude Code not set up: bots think with it.",
    ]);
  });
  test("Claude Code installed but signed out", () => {
    expect(setupIssues({ chrome: { found: true, path: "x" }, claude: { found: true, signed_in: false } })).toEqual([
      "Claude Code not set up: sign in to it first.",
    ]);
  });
});

describe("the Install Chrome button", () => {
  const plan = { available: true, reason: "", command: "wget … && sudo apt-get install -y ./chrome.deb" };
  test("offered only while Chrome is missing and the server can install it", () => {
    expect(chromeInstallPlan({ chrome: { found: false, path: null, wsl: true, install: plan }, claude: {} })).toEqual(plan);
    expect(chromeInstallPlan({ chrome: { found: false, path: null, wsl: true, install: { ...plan, available: false, reason: "no apt-get" } }, claude: {} })).toBeNull();
    expect(chromeInstallPlan({ chrome: { found: false, path: null }, claude: {} })).toBeNull();  // macOS, or an older server
    expect(chromeInstallPlan({ chrome: { found: true, path: "/usr/bin/google-chrome", install: plan }, claude: {} })).toBeNull();
    expect(chromeInstallPlan(null)).toBeNull();
  });
  test("shown under a thread that ends on the no-browser error, and only there", () => {
    const err = { role: "error" as const, text: "RuntimeError: No Chrome, Chromium, Edge or Brave browser found inside WSL. Bots launch their own browser" };
    const said = { role: "user" as const, text: "can you access DuckDB CLI or Python." };
    expect(endsOnNoBrowser([said, err])).toBe(true);
    expect(endsOnNoBrowser([said, err, said])).toBe(false);  // asked again: the error is history
    expect(endsOnNoBrowser([said, { role: "error" as const, text: "Busy with another task; send again when it's done." }])).toBe(false);
    expect(endsOnNoBrowser([{ role: "user" as const, text: err.text }])).toBe(false);  // the words quoted by the person
    expect(endsOnNoBrowser([])).toBe(false);
  });
});
