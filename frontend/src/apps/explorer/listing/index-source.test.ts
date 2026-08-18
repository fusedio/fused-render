import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  MAX_SCANNING_POLLS,
  UNCOVERED_GRACE,
  nextStep,
} from "@apps/explorer/listing/index-source";

const at = (over: Partial<Parameters<typeof nextStep>[0]> = {}) =>
  nextStep({ reason: "", asked: false, sinceAsk: 0, polls: 0, ...over });

describe("what to do with a ranked answer", () => {
  test("a covered folder is simply answered", () => {
    expect(at({ reason: "" })).toBe("answer");
  });

  test("the folders no scan can ever cover go to the live walk", () => {
    // The one client rule, and it is not a copy of the mount policy: the
    // client never works out WHY, it walks when the server has said it cannot
    // answer and cannot be made to.
    expect(at({ reason: "mount" })).toBe("walk");
    expect(at({ reason: "package" })).toBe("walk");
    expect(at({ reason: "ignored" })).toBe("walk");
  });

  test("an uncovered folder is scanned, once", () => {
    expect(at({ reason: "uncovered" })).toBe("scan");
    expect(at({ reason: "uncovered", asked: true })).toBe("poll");
  });

  test("a scan in flight is polled", () => {
    expect(at({ reason: "scanning" })).toBe("poll");
    expect(at({ reason: "scanning", asked: true, sinceAsk: 9 })).toBe("poll");
  });

  test("a folder still uncovered after its scan falls back to the walk", () => {
    // The scan ran and the folder is STILL not in the index — it is on another
    // filesystem, or the scan failed. Asking again would be the retry loop the
    // whole design refuses; answering "no matches" would blame the user's
    // files for the app's state. The walk is what is left.
    expect(at({ reason: "uncovered", asked: true, sinceAsk: UNCOVERED_GRACE }))
      .toBe("walk");
  });

  test("the grace period exists because a scan takes a moment to be visible", () => {
    // `runner.start` returns before the run is listed as live, so the next
    // answer can still read `uncovered`. Giving up on that one would abandon
    // every on-demand scan the instant it was asked for.
    for (let i = 0; i < UNCOVERED_GRACE; i++) {
      expect(at({ reason: "uncovered", asked: true, sinceAsk: i })).toBe("poll");
    }
  });

  test("polling a scan stops being useful eventually", () => {
    // A whole-home scan can run for minutes. The rows in hand are real; going
    // on asking for them at a fixed cadence for the length of a scan is not
    // what the poll is for.
    expect(at({ reason: "scanning", polls: MAX_SCANNING_POLLS - 1 })).toBe("poll");
    expect(at({ reason: "scanning", polls: MAX_SCANNING_POLLS })).toBe("answer");
  });

  test("a probe answer cannot count as the on-demand scan's first look", () => {
    // The focus probe runs this with asked:false, so it can only ever come
    // back "walk" or "answer"/"scan" — never the give-up branch.
    expect(at({ reason: "uncovered", asked: false, sinceAsk: 99 })).toBe("scan");
  });

  test("an unknown reason is answered, not walked", () => {
    // Forward compatibility: a server that grows a reason this build has never
    // heard of has still ANSWERED, and its hits are on screen.
    expect(at({ reason: "something-new" as never })).toBe("answer");
  });
});

// -- how the hook is wired to it ------------------------------------------------
//
// Source guards. The suite has no DOM, so what is testable about the hook is
// the mechanism — and each of these is a way the cutover could regress into
// exactly what it replaced.

const HOOK = readFileSync(join(import.meta.dir, "useWalkSearch.ts"), "utf8");

describe("useWalkSearch's half of the decision", () => {
  test("the hook never spells out which reasons mean the walk", () => {
    // One place knows that, and it is this module. A second copy in the hook
    // is how the client ends up with its own mount policy again.
    for (const literal of ['"mount"', '"package"', '"ignored"', '"uncovered"']) {
      expect(HOOK).not.toContain("=== " + literal);
    }
    expect(HOOK).toContain("nextStep(");
  });

  test("the live walk runs only when the decision says so", () => {
    // The walk is the fallback for folders no scan can cover, not a second
    // source racing the first: exactly one call, gated on walkMode.
    const calls = HOOK.split("\n").filter((l) => l.includes("walkDirStream("));
    expect(calls).toHaveLength(1);
    expect(HOOK).toContain("if (!walkMode || walkReq === null) return;");
  });

  test("a scan is asked for once, from the step that says to", () => {
    const calls = HOOK.split("\n").filter((l) => l.includes("requestFolderScan("));
    expect(calls).toHaveLength(1);
    expect(HOOK).toContain('if (step === "scan") {');
  });

  test("a refused scan is not retried — it hands over to the walk", () => {
    // The one shape of retry loop this route can produce: the server refuses
    // (mount-backed, gone, scanned too recently), the box reads it as
    // transient, and asks again on the next keystroke.
    const scan = HOOK.slice(HOOK.indexOf('if (step === "scan") {'),
                            HOOK.indexOf('if (step === "poll")'));
    expect(scan).toContain("if (!r.started)");
    expect(scan).toContain("setWalkMode(true)");
  });

  test("the memo is not consulted while a scan is landing rows", () => {
    // A remembered answer taken mid-scan is precisely the one that is out of
    // date, and serving it would freeze the trickle the poll exists to show.
    expect(HOOK).toContain("const remembered = polling ? undefined : memo.current.get(q)");
  });

  test("the ranked rows are rendered under whatever query they answer", () => {
    // The never-blank rule: query-tagged holding (lib/search-hold) is the
    // walk's, where re-ranking is free. Routing the ranked answer through it
    // would blank the list on every keystroke.
    expect(HOOK).toContain("const indexRows = searching && !walkMode ? (answer?.hits ?? []) : []");
    expect(HOOK).toContain("const displayHits = walkMode ? walkDisplay.hits : indexRows;");
  });
});
