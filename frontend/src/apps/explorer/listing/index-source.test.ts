import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  MAX_SCANNING_POLLS,
  UNCOVERED_GRACE,
  nextStep,
  remembersAnswer,
  searchProgress,
} from "@apps/explorer/listing/index-source";

const at = (over: Partial<Parameters<typeof nextStep>[0]> = {}) =>
  nextStep({ reason: "", asked: false, sinceAsk: 0, polls: 0, ...over });

describe("what to do with a ranked answer", () => {
  test("a covered folder is simply answered", () => {
    expect(at({ reason: "" })).toBe("answer");
  });

  test("the folders no scan can ever cover are answered with the index gap", () => {
    // The one client rule, and it is not a copy of the mount policy: the
    // client never works out WHY, it reports the gap when the server has said
    // it cannot answer and cannot be made to.
    expect(at({ reason: "mount" })).toBe("answer");
    expect(at({ reason: "package" })).toBe("answer");
    expect(at({ reason: "ignored" })).toBe("answer");
  });

  test("a disabled index is answered too, never scanned or polled", () => {
    // If this asked for a scan, it would burn UNCOVERED_GRACE against one
    // that will never start; if it polled, it would burn all 80 polls
    // against one that will never end. There is no server signal to poll
    // for "the user turned indexing back on".
    expect(at({ reason: "disabled" })).toBe("answer");
    expect(at({ reason: "disabled", asked: true, sinceAsk: 99 })).toBe("answer");
    expect(at({ reason: "fda" })).toBe("answer");
  });

  test("an uncovered folder is scanned, once", () => {
    expect(at({ reason: "uncovered" })).toBe("scan");
    expect(at({ reason: "uncovered", asked: true })).toBe("poll");
  });

  test("a scan in flight is polled", () => {
    expect(at({ reason: "scanning" })).toBe("poll");
    expect(at({ reason: "scanning", asked: true, sinceAsk: 9 })).toBe("poll");
  });

  test("a folder still uncovered after its scan settles for the index gap", () => {
    // The scan ran and the folder is STILL not in the index — it is on another
    // filesystem, or the scan failed. Asking again would be the retry loop the
    // whole design refuses; answering "no matches" would blame the user's
    // files for the app's state. The index gap is what is left.
    expect(at({ reason: "uncovered", asked: true, sinceAsk: UNCOVERED_GRACE }))
      .toBe("answer");
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
    // back "scan" or "answer" — never the give-up branch.
    expect(at({ reason: "uncovered", asked: false, sinceAsk: 99 })).toBe("scan");
  });

  test("an unknown reason is answered", () => {
    // Forward compatibility: a server that grows a reason this build has never
    // heard of has still ANSWERED, and its hits are on screen.
    expect(at({ reason: "something-new" as never })).toBe("answer");
  });
});

// -- the one rule that is genuinely about the SOURCE ---------------------------
//
// Everything else this describe used to assert — the epoch tag, the poll
// counting its ticks, the in-flight guard, the memo predicate, which rows are
// rendered — is driven in useListingSearch.render.test.ts, where a wrong
// condition fails instead of a renamed one. What stays is the rule that has no
// runtime shadow: WHERE the policy lives. A hook that starts switching on
// reasons itself would pass every behavioural test in this directory and still
// be the bug this phase set out to remove, because the drift only shows up
// when the server's rules change.

const HOOK = readFileSync(join(import.meta.dir, "useListingSearch.ts"), "utf8");

describe("who decides which source answers", () => {
  test("the hook never spells out which reasons are permanently uncoverable", () => {
    for (const literal of ['"mount"', '"package"', '"ignored"', '"uncovered"']) {
      expect(HOOK).not.toContain("=== " + literal);
    }
    expect(HOOK).toContain("nextStep(");
  });
});

// -- what the box is allowed to remember ---------------------------------------

describe("remembersAnswer", () => {
  test("a settled, covered answer is worth remembering", () => {
    expect(remembersAnswer("answer", "")).toBe(true);
  });

  test("an answer taken mid-scan is not", () => {
    // It is a snapshot of a folder still being indexed; serving it back on a
    // backspace would freeze the trickle the poll exists to show.
    expect(remembersAnswer("poll", "scanning")).toBe(false);
    expect(remembersAnswer("scan", "uncovered")).toBe(false);
    // ...including the one that settles only because the poll ceiling ran out.
    expect(remembersAnswer("answer", "scanning")).toBe(false);
  });

  test("nothing is remembered for a folder the index cannot cover", () => {
    expect(remembersAnswer("answer", "mount")).toBe(false);
  });
});

// -- is an answer still coming, and are these rows momentary? ------------------

describe("searchProgress", () => {
  const p = (over: Partial<Parameters<typeof searchProgress>[0]> = {}) =>
    searchProgress({ searching: true, pending: false, polling: false, ...over });

  test("a scan landing rows means an answer is still coming", () => {
    // THE regression: the first uncovered answer clears `pending` while the
    // on-demand scan runs, and an empty list then read as a finished zero-hit
    // result — "No matches" for the whole window the scan is working in.
    expect(p({ polling: true }).answerComing).toBe(true);
    expect(p({ pending: true }).answerComing).toBe(true);
    expect(p().answerComing).toBe(false);
  });

  test("a scan is not a MOMENTARY state, so it does not drive the heavy dim", () => {
    // The two dims say different things: `inFlight` is a round trip that
    // clears in a moment, and a scan that runs for ten seconds is the caveat's
    // job ("indexing…"), not a dim calibrated for a moment.
    expect(p({ polling: true }).inFlight).toBe(false);
    expect(p({ pending: true }).inFlight).toBe(true);
  });

  test("nothing is coming when nobody is searching", () => {
    expect(p({ searching: false, pending: true, polling: true }).answerComing).toBe(false);
    expect(p({ searching: false, pending: true }).inFlight).toBe(false);
  });
});
