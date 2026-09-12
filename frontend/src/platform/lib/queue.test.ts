// The project queue's ONE caption builder. Three surfaces print what this
// returns — a Tasks List row, a Tasks Board card and the native chat's chip —
// and the whole reason it is a pure function in `platform` is that two of those
// are shell and one is an app, which may not read shell. So these tests are
// about the WORDS, because the words are the shared thing.
import { describe, expect, it } from "bun:test";
import {
  QUEUE_PRIORITY_GLYPH,
  queueLine,
  queuePosition,
  queueRunsNext,
} from "./queue";

describe("queueLine", () => {
  it("says where it stands and who is in front", () => {
    expect(
      queueLine({
        status: "queued",
        queue_position: 2,
        queue_ahead: "TASK-041",
        queue_ahead_title: "Pull today's news",
      }),
    ).toMatchObject({
      head: "#2 in line",
      behind: "behind TASK-041",
      text: "#2 in line · behind TASK-041",
      runsNext: false,
      aheadTitle: "Pull today's news",
    });
  });

  it("names SOMETHING when the holder has no id — never a sentence that stops", () => {
    // A folder can be held by a run with no task row: a scheduler entry already
    // claimed, a transcript that has gone. "behind" followed by nothing reads as
    // a bug; "behind a run in this folder" is the honest answer and is still a
    // sentence.
    expect(queueLine({ status: "queued", queue_position: 3, queue_ahead: "" })?.text)
      .toBe("#3 in line · behind a run in this folder");
    expect(queueLine({ status: "queued", queue_position: 3 })?.text)
      .toBe("#3 in line · behind a run in this folder");
    // Whitespace is not an id either.
    expect(queueLine({ status: "queued", queue_position: 3, queue_ahead: "  " })?.text)
      .toBe("#3 in line · behind a run in this folder");
  });

  it("reads 'runs next' ONLY for a spot that has been claimed", () => {
    // A position is where this stood when the server last looked; anything else
    // in the folder can be skipped over it a second later. Only `queue_priority`
    // is a claim — so #1 without it is still "#1 in line", with a live Skip that
    // turns it into the other sentence.
    const ahead = { status: "queued", queue_ahead: "TASK-041" };
    expect(queueLine({ ...ahead, queue_position: 1 })?.head).toBe("#1 in line");
    expect(queueLine({ ...ahead, queue_position: 1 })?.runsNext).toBe(false);
    expect(queueLine({ ...ahead, queue_position: 1, queue_priority: true })?.head)
      .toBe("runs next");
    expect(queueLine({ ...ahead, queue_position: 9, queue_priority: true })?.head)
      .toBe("runs next");
    // …and it is still BEHIND something: skipping the queue never interrupts the
    // run holding the folder, and the caption must not imply that it did.
    expect(queueLine({ ...ahead, queue_position: 9, queue_priority: true })?.text)
      .toBe("runs next · behind TASK-041");
  });

  it("says 'in line' with no number when the server could not place it", () => {
    // Position 0 is "I could not say", and the views may NOT round that up to
    // first: a chip reading "runs next" over work that is actually fifth is the
    // one thing this caption can get actively wrong. No number, no ⤒ — and, the
    // part the reader can act on, Skip stays live (see `queueRunsNext`).
    expect(
      queueLine({ status: "queued", queue_position: 0, queue_ahead: "TASK-041" }),
    ).toMatchObject({
      head: "in line",
      text: "in line · behind TASK-041",
      runsNext: false,
    });
    expect(queueLine({ status: "queued", queue_ahead: "" })?.text).toBe(
      "in line · behind a run in this folder",
    );
    // …unless the server DID say this one goes out next, which it can say
    // without a number: a skip sets the flag and the position follows later.
    expect(queueLine({ status: "queued", queue_position: 0, queue_priority: true })?.head)
      .toBe("runs next");
  });

  it("names the reader's OWN earlier message when that is what is in front", () => {
    // `behind_own` — a follow-up typed into a chat whose first message is still
    // waiting (admit's `follow_of`). The FOLDER may be free; what holds this one
    // is the line above it, so "behind TASK-041" would be a sentence about a
    // stranger's run and "behind a run in this folder" a sentence about a folder
    // that is not busy.
    //
    // AND NEVER A "#n" IN FRONT OF IT, placed or not (browser QA round 2): the
    // phrase already IS the position — it names the exact thing this is behind
    // — so "#1 in line · after your previous message" says it twice, and
    // "#3 in line · after your previous message" says two different things,
    // because the server's number counts a folder's line that also holds
    // strangers' tasks the reader is not standing behind.
    expect(
      queueLine({ status: "queued", queue_position: 2, behind_own: true }),
    ).toMatchObject({
      head: "",
      behind: "after your previous message",
      text: "after your previous message",
      runsNext: false,
    });
    expect(
      queueLine({ status: "queued", queue_position: 1, behind_own: true })?.text,
    ).toBe("after your previous message");
    // The ordinary case — a follower's place is not known until its leader has
    // run — reads exactly the same, which is the point.
    expect(queueLine({ status: "queued", behind_own: true })).toMatchObject({
      head: "",
      text: "after your previous message",
    });
    // It outranks a task id the server sent anyway: the specific sentence wins.
    expect(
      queueLine({ status: "queued", behind_own: true, queue_ahead: "TASK-041" })?.text,
    ).toBe("after your previous message");
    // A skipped follower still reads as the head, and still names what it is
    // behind — skipping never interrupts anything. "runs next" is the ONE head
    // a follow-up keeps, because it is a claim about the spot rather than a
    // count of a line.
    expect(
      queueLine({ status: "queued", behind_own: true, queue_priority: true })?.text,
    ).toBe("runs next · after your previous message");
    expect(
      queueLine({
        status: "queued",
        behind_own: true,
        queue_position: 4,
        queue_priority: true,
      })?.text,
    ).toBe("runs next · after your previous message");
    // ABSENT IS THE ROW'S CASE, and it must read exactly as it always has: a
    // `/api/tasks` row never carries this (a row is about a TASK's place in its
    // folder; whose message is in front is a fact about one MESSAGE), so the
    // Board card and the List row are untouched by the field existing.
    expect(queueLine({ status: "queued", queue_ahead: "TASK-041" })?.text).toBe(
      "in line · behind TASK-041",
    );
    expect(queueLine({ status: "queued", behind_own: false, queue_ahead: "" })?.text).toBe(
      "in line · behind a run in this folder",
    );
  });

  it("says nothing at all about a task that is not queued", () => {
    // The three surfaces draw NOTHING on a null, so this is the gate that keeps
    // the caption off every other row on the page.
    for (const status of ["upcoming", "in_progress", "done", "archived", "", "weird"]) {
      expect(queueLine({ status, queue_position: 2, queue_ahead: "TASK-041" })).toBe(null);
    }
    expect(queueLine({ queue_position: 2 })).toBe(null);
  });
});

describe("queuePosition", () => {
  it("treats anything that is not a real place as no place at all", () => {
    // 0 is what an older server sends, and what a row the server could not
    // place carries. It is NOT "#0", and it is not the head either — the
    // callers print it as a bare "in line".
    expect(queuePosition({ queue_position: 4 })).toBe(4);
    expect(queuePosition({})).toBe(0);
    expect(queuePosition({ queue_position: 0 })).toBe(0);
    expect(queuePosition({ queue_position: -2 })).toBe(0);
    expect(queuePosition({ queue_position: Number.NaN })).toBe(0);
    expect(queuePosition({ queue_position: Number.POSITIVE_INFINITY })).toBe(0);
    // A float is a server bug, not a reason to print "#2.5 in line".
    expect(queuePosition({ queue_position: 2.7 })).toBe(2);
  });
});

describe("queueRunsNext", () => {
  it("is the priority flag, and is never inferred from a position", () => {
    // The ⤒ glyph and a DEAD Skip button both hang off this, so every position
    // that read as the head was a row told "runs next" about a spot it had not
    // claimed — with the one control that would have claimed it taken away
    // (browser QA, 2026-09-12).
    expect(queueRunsNext({ queue_priority: true })).toBe(true);
    expect(queueRunsNext({ queue_position: 5, queue_priority: true })).toBe(true);
    expect(queueRunsNext({ queue_position: 1 })).toBe(false);
    expect(queueRunsNext({ queue_position: 2 })).toBe(false);
    expect(queueRunsNext({})).toBe(false);
    expect(queueRunsNext({ queue_position: 0 })).toBe(false);
    expect(queueRunsNext({ queue_position: 1, queue_priority: false })).toBe(false);
  });
});

describe("the priority glyph", () => {
  it("means 'to the top of this' and not 'now'", () => {
    // An arrow to a bar. A bolt or a star would promise the one thing skipping
    // never does: interrupt the run holding the folder.
    expect(QUEUE_PRIORITY_GLYPH).toBe("⤒");
    expect(QUEUE_PRIORITY_GLYPH).not.toContain("⚡");
  });
});
