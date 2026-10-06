import { describe, expect, test } from "bun:test";
import { setupIssues } from "./setup";

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
