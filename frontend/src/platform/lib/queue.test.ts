// THE PROJECT QUEUE'S WORDS (platform/lib/queue.ts).
//
// Every string three surfaces say about one waiting task is built here, so this
// is where the wording is held still. The vocabulary was replaced wholesale on
// 2026-09-12 (Akshil) and each test below names the sentence it retired, because
// a caption that reads fine in isolation is exactly the kind of thing that drifts
// back.
import { describe, expect, it } from "bun:test";
import {
  canRunNext,
  chatUrl,
  NEXT_IN_FOLDER,
  QUEUE_PRIORITY_GLYPH,
  queueAheadHref,
  queueBehind,
  queueCaption,
  queueOrdinal,
  queuePosition,
  queueRunsNext,
  RUN_NEXT_DONE_HINT,
  RUN_NEXT_HINT,
  RUN_NEXT_LABEL,
  runningWaitingLabel,
  waitingCardText,
  waitingCount,
  waitingLabel,
} from "./queue";

const queued = (extra: Record<string, unknown> = {}) => ({ status: "queued", ...extra });

describe("a place in the line", () => {
  it("is an ORDINAL, the way a person says it out loud", () => {
    // "#2 in line" was the shipped wording and it is a database row number.
    expect(queueCaption(queued({ queue_position: 1 }))?.text).toBe("1st in line");
    expect(queueCaption(queued({ queue_position: 2 }))?.text).toBe("2nd in line");
    expect(queueCaption(queued({ queue_position: 3 }))?.text).toBe("3rd in line");
    expect(queueCaption(queued({ queue_position: 4 }))?.text).toBe("4th in line");
  });

  it("gets the teens right, which is the only reason it is a function", () => {
    // 11/12/13 take "th" where 21/22/23 do not, and a folder twelve deep is not
    // a hypothetical.
    expect(queueOrdinal(11)).toBe("11th");
    expect(queueOrdinal(12)).toBe("12th");
    expect(queueOrdinal(13)).toBe("13th");
    expect(queueOrdinal(21)).toBe("21st");
    expect(queueOrdinal(22)).toBe("22nd");
    expect(queueOrdinal(23)).toBe("23rd");
    expect(queueOrdinal(112)).toBe("112th");
  });

  it("says a bare 'in line' when the server could not place it, never '0th'", () => {
    expect(queueOrdinal(0)).toBe("");
    expect(queueOrdinal(-3)).toBe("");
    expect(queuePosition({ queue_position: 0 })).toBe(0);
    expect(queueCaption(queued())?.text).toBe("in line");
    expect(queueCaption(queued({ queue_position: 0 }))?.text).toBe("in line");
  });

  it("has nothing to say about a row that is not queued", () => {
    expect(queueCaption({ status: "in_progress", queue_position: 2 })).toBe(null);
    expect(queueCaption({})).toBe(null);
  });
});

describe("what is in front", () => {
  it("names a task ONLY when a different one is holding the folder", () => {
    expect(queueBehind({ queue_ahead: "TASK-038" })).toBe("behind TASK-038");
    expect(queueCaption(queued({ queue_position: 1, queue_ahead: "TASK-038" }))?.text).toBe(
      "1st in line · behind TASK-038",
    );
  });

  it("says NOTHING at all when there is no name to give", () => {
    // "behind a run in this folder" was the shipped empty case, and it is a
    // sentence with a hole in it: nothing to look at, nothing to press, and a
    // reader who has just typed into their own chat being told about a stranger
    // who may not exist. The folder is often simply free.
    expect(queueBehind({ queue_ahead: "" })).toBe("");
    expect(queueBehind({})).toBe("");
    const line = queueCaption(queued({ queue_position: 2 }));
    expect(line?.text).toBe("2nd in line");
    expect(line?.text).not.toContain("behind");
  });

  it("keeps the holder's title OFF the caption and ON the pointer", () => {
    // `behind TASK-041 "Pull today's news"` was a second sentence nested inside
    // the first, and it was the first thing to push the one actionable token off
    // the end of a 340px row.
    const line = queueCaption(
      queued({ queue_position: 1, queue_ahead: "TASK-038", queue_ahead_title: "Pull the news" }),
    );
    expect(line?.text).toBe("1st in line · behind TASK-038");
    expect(line?.text).not.toContain("Pull the news");
    expect(line?.aheadTitle).toBe("Pull the news");
  });

  it("hands back where that id GOES, and null when there is nowhere", () => {
    const href = queueAheadHref({
      queue_ahead: "TASK-038",
      queue_ahead_session: "sess-1",
      queue_ahead_target: "/Users/me/app",
    });
    expect(href).toBe("/explorer/view/Users/me/app?_side=claude&session_id=sess-1");
    // ONE CODEC: the shell's explorerUrl delegates to this, so the app layer and
    // the shell cannot disagree about where a conversation lives.
    expect(href).toBe(chatUrl("/Users/me/app", "sess-1"));
    // An older server sends neither half. Plain text beats a link to nothing.
    expect(queueAheadHref({ queue_ahead: "TASK-038" })).toBe(null);
    expect(queueAheadHref({ queue_ahead_session: "sess-1" })).toBe(null);
    expect(queueAheadHref({ queue_ahead_target: "/Users/me/app" })).toBe(null);
  });

  it("encodes a path with spaces and a Windows drive", () => {
    expect(chatUrl("/Users/me/my app", "s")).toContain("/explorer/view/Users/me/my%20app");
    expect(chatUrl("C:\\work\\app", "s")).toContain("/explorer/view/C%3A/work/app");
  });
});

describe("runs next", () => {
  it("is the PRIORITY FLAG and never a position", () => {
    // Standing 1st is where this stood when the server last looked; anything in
    // the folder can be skipped over it in the next second. Only the flag is a
    // claim on the spot, and reading 1st as the head took Run next away from the
    // row that most wanted to press it (browser QA, 2026-09-12).
    expect(queueRunsNext({ queue_position: 1 })).toBe(false);
    expect(queueRunsNext({ queue_priority: true })).toBe(true);
    expect(queueCaption(queued({ queue_position: 1 }))?.runsNext).toBe(false);
    expect(queueCaption(queued({ queue_position: 1, queue_priority: true }))?.runsNext).toBe(true);
  });

  it("is what the Run next press is offered for, and only that", () => {
    // Something else in front AND the spot not already claimed. Either half
    // missing and the press could only put the reader back where they are.
    expect(canRunNext({ queue_ahead: "TASK-038" })).toBe(true);
    expect(canRunNext({ queue_ahead: "TASK-038", queue_priority: true })).toBe(false);
    expect(canRunNext({ queue_ahead: "" })).toBe(false);
  });

  it("is the verb every surface says, in one place", () => {
    // "Skip the queue" read as skipping the MESSAGE. The press makes the message
    // RUN, next — and interrupts nothing, which is the half the hint says aloud.
    expect(RUN_NEXT_LABEL).toBe("Run next");
    expect(RUN_NEXT_HINT).toContain("nothing is interrupted");
    expect(RUN_NEXT_HINT).not.toContain("Skip");
    expect(RUN_NEXT_DONE_HINT).toBe("Already next in this folder");
    // The mark means "to the top of this" and not "faster" — a bolt would promise
    // the one thing this feature must never be read as offering.
    expect(QUEUE_PRIORITY_GLYPH).toBe("⤒");
  });
});

describe("counting what is waiting", () => {
  it("says the noun once, where the noun is spoken", () => {
    expect(waitingCount(1)).toBe("1 message waiting");
    expect(waitingCount(2)).toBe("2 messages waiting");
  });

  it("says 'waiting' and not 'queued' wherever a person is being told a number", () => {
    // `queued` is the STATUS WORD — the enum, the ring, the filter. A count
    // beside "1 running" is a different register (Akshil, 2026-09-12).
    expect(waitingLabel(2)).toBe("2 waiting");
    expect(runningWaitingLabel(1, 2)).toBe("1 running · 2 waiting");
    // Either half alone when the other is empty, so a lane with nothing waiting
    // reads exactly as it always did.
    expect(runningWaitingLabel(3, 0)).toBe("3 running");
    expect(runningWaitingLabel(0, 3)).toBe("3 waiting");
    expect(runningWaitingLabel(0, 0)).toBe("");
  });

  it("builds the chat card's whole sentence, in its three states", () => {
    expect(waitingCardText(1, { queue_ahead: "TASK-038" })).toBe(
      "1 message waiting · behind TASK-038",
    );
    // After Run next: the spot is claimed, so nothing is in front any more even
    // though TASK-038 is still holding the folder.
    expect(waitingCardText(2, { queue_ahead: "TASK-038", queue_priority: true })).toBe(
      "2 messages waiting · next in this folder",
    );
    // …and the same sentence for a chat whose folder was never busy at all: one
    // wording for one fact, whichever road reached it.
    expect(waitingCardText(2, {})).toBe("2 messages waiting · next in this folder");
    expect(NEXT_IN_FOLDER).toBe("next in this folder");
  });
});

describe("the retired vocabulary", () => {
  it("is gone from the module, not merely unused by its callers", async () => {
    // A builder left exported is a builder a later surface picks up, and the
    // whole point of this file is that there is one wording.
    const mod = await import("./queue");
    for (const dead of ["queueLine", "quoteAhead", "QUEUE_AHEAD_TITLE_MAX"]) {
      expect(dead in mod).toBe(false);
    }
  });
});
