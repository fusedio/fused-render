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
  nextStep({ reason: "", asked: false, sinceAsk: 0, polls: 0, covered: true, ...over });

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

  test("giving up on a scan that never covered the folder goes to the walk", () => {
    // The other door into "blame the user's files for the app's state". A scan
    // of an UNCOVERED root reports `scanning` too, so settling for what we have
    // at the ceiling would render covered:false, hits:[] — an empty list for a
    // folder the walk would have searched fine.
    expect(at({ reason: "scanning", polls: MAX_SCANNING_POLLS, covered: false }))
      .toBe("walk");
    // ...while a covered folder really does have rows worth settling for.
    expect(at({ reason: "scanning", polls: MAX_SCANNING_POLLS, covered: true }))
      .toBe("answer");
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

  test("a reply that outlived its folder or generation is dropped", () => {
    // The two requests that outlive the effect that issued them — the scan ask
    // and the focus probe — both end in setWalkMode(true), and this hook is
    // NOT remounted per folder. Without the epoch tag, a `refused` for the
    // folder you just left pins the folder you just opened to the live walk,
    // and only a generation change ever clears that.
    const asks = HOOK.slice(HOOK.indexOf('if (step === "scan") {'),
                            HOOK.indexOf('if (step === "poll")'));
    expect(asks).toContain("const epoch = sourceEpoch.current;");
    expect(asks.match(/if \(sourceEpoch\.current !== epoch\) return;/g)).toHaveLength(2);
    const probe = HOOK.slice(HOOK.indexOf("const probeKey ="),
                             HOOK.indexOf("// Debounced URL mirror"));
    expect(probe).toContain("if (sourceEpoch.current !== epoch) return;");
    // ...and the epoch has to actually move with the folder and generation.
    expect(HOOK).toContain("sourceEpoch.current += 1;");
  });

  test("the poll counts its own ticks, not the answers it gets back", () => {
    // A tick aborts the request in flight, so a rank that consistently
    // outlasts the interval produces no answers — and a ceiling counted in
    // answers is one the loop can starve, leaving a 1.5s request loop running
    // long after the scan it was waiting for finished.
    const timer = HOOK.slice(HOOK.indexOf("  useEffect(() => {\n    if (!polling"),
                             HOOK.indexOf("--- the live walk"));
    expect(timer).toContain("polls.current += 1;");
    expect(timer).toContain("nextStep(");
    // ...and nothing else increments it.
    expect(HOOK.split("\n").filter((l) => l.includes("polls.current += 1"))).toHaveLength(1);
  });

  test("a poll tick never aborts a request that is still out", () => {
    expect(HOOK).toContain("if (inflightKey.current === key) return;");
  });

  test("the memo is not consulted while a scan is landing rows", () => {
    // A remembered answer taken mid-scan is precisely the one that is out of
    // date, and serving it would freeze the trickle the poll exists to show.
    expect(HOOK).toContain("const remembered = polling ? undefined : memo.current.get(q)");
    // ...and the WRITE side asks the pure rule rather than the same flag,
    // which is a commit behind at exactly the moment a scan ends.
    expect(HOOK).toContain('remembersAnswer(step, res.reason ?? "")');
  });

  test("the ranked rows are rendered under whatever query they answer", () => {
    // The never-blank rule: query-tagged holding (lib/search-hold) is the
    // walk's, where re-ranking is free. Routing the ranked answer through it
    // would blank the list on every keystroke.
    expect(HOOK).toContain("const indexRows = searching && !walkMode ? (answer?.hits ?? []) : []");
    expect(HOOK).toContain("const displayHits = walkMode ? walkDisplay.hits : indexRows;");
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

  test("nothing is remembered for a folder handed to the walk", () => {
    expect(remembersAnswer("walk", "mount")).toBe(false);
  });
});

// -- is an answer still coming, and are these rows momentary? ------------------

describe("searchProgress", () => {
  const p = (over: Partial<Parameters<typeof searchProgress>[0]> = {}) =>
    searchProgress({ searching: true, walkMode: false, pending: false,
                     polling: false, scanning: false, ...over });

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

  test("the walk reports its own scoring pass, and never the poll", () => {
    // A walk-backed folder has no ranked request and no scan to wait for.
    expect(p({ walkMode: true, scanning: true }).answerComing).toBe(true);
    expect(p({ walkMode: true, scanning: true }).inFlight).toBe(true);
    expect(p({ walkMode: true, polling: true, pending: true }).answerComing).toBe(false);
  });

  test("nothing is coming when nobody is searching", () => {
    expect(p({ searching: false, pending: true, polling: true }).answerComing).toBe(false);
    expect(p({ searching: false, pending: true }).inFlight).toBe(false);
  });
});
