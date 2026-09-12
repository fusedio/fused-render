// THE ROWS A CHAT DRAWS FOR MESSAGES IT HAS NOT SENT YET (sched/waiting.ts).
//
// The one rule this file exists to hold: THE ROWS COME FROM THE SERVER. The chip
// this replaced was client state, so a reload — or the session adoption this very
// feature causes — dropped every card while the entries sat in the line, and the
// reader's messages were safe on the server and invisible on screen. Everything
// below is a test about that swap, its edges and its one honest exception (the
// window before the first poll has seen a brand-new entry).
import { describe, expect, it } from "bun:test";
import {
  EMPTY_SEED_WATCH,
  pruneDropped,
  reconcileSeeds,
  waitingFacts,
  waitingLine,
  waitingRows,
  waitingWhen,
  waitingWord,
  WAITING_UNSEEN_POLLS,
  NO_DROPPED,
} from "./waiting";
import type { SchedEntry } from "./scheduled";

const AT = Date.parse("2026-09-12T10:00:00Z");
const entry = (id: string, extra: Partial<SchedEntry> = {}): SchedEntry => ({
  id,
  state: "pending",
  due: "2026-09-12T09:59:00Z",
  message: "from the server",
  ...extra,
});
const seed = (id: string, text = "typed here", due = "2026-09-12T09:59:30Z") => ({
  entryId: id,
  text,
  due,
});

describe("the rows are the server's", () => {
  it("draws one row per pending entry, in the order the server gave", () => {
    const rows = waitingRows([entry("a"), entry("b")], [], NO_DROPPED, AT);
    expect(rows.map((r) => r.entryId)).toEqual(["a", "b"]);
    expect(rows.every((r) => r.optimistic)).toBe(false);
  });

  it("shows a seed the server has not listed YET — and only until it does", () => {
    // The window between the admission creating the entry and the next 15 s tick
    // listing it. Without the seed, a message the reader just pressed Enter on
    // would be nowhere on screen for up to a poll interval, which reads as a
    // message that went nowhere.
    const before = waitingRows([], [seed("a")], NO_DROPPED, AT);
    expect(before.map((r) => r.entryId)).toEqual(["a"]);
    expect(before[0].optimistic).toBe(true);
    // …and once it is listed, ONE row, the server's.
    const after = waitingRows([entry("a")], [seed("a")], NO_DROPPED, AT);
    expect(after).toHaveLength(1);
    expect(after[0].optimistic).toBe(false);
  });

  it("keeps the TYPED line across that swap, so the row does not rewrite itself", () => {
    // The stored entry holds the COMPOSED message — attachment markers and all —
    // and the reader typed a line. Swapping one for the other on the next poll
    // would be the bubble silently changing its words under them.
    const rows = waitingRows([entry("a")], [seed("a", "hello there")], NO_DROPPED, AT);
    expect(rows[0].text).toBe("hello there");
    // With no seed (a reload — the memory is gone, the entry is not) the server's
    // own message is what there is, and it is a true rendering of the message.
    expect(waitingRows([entry("a")], [], NO_DROPPED, AT)[0].text).toBe("from the server");
  });

  it("puts the server's rows first and the unlisted seeds after them", () => {
    // The server's list is already in due order, which for a chat's own sends is
    // the order they were typed; a seed no poll has listed is by construction the
    // newest thing in the conversation.
    const rows = waitingRows([entry("a"), entry("b")], [seed("c")], NO_DROPPED, AT);
    expect(rows.map((r) => r.entryId)).toEqual(["a", "b", "c"]);
  });

  it("never draws a row a delete took back, from EITHER side", () => {
    // The poll's own list is a photograph taken up to a lap before the press, so
    // filtering the seeds alone would put the row straight back up over the very
    // message the reader had just deleted.
    const dropped = new Set(["a"]);
    expect(waitingRows([entry("a"), entry("b")], [seed("a")], dropped, AT).map((r) => r.entryId))
      .toEqual(["b"]);
  });
});

describe("the state word", () => {
  it("is `queued` once the time has come and `scheduled` before it", () => {
    expect(waitingWord("2026-09-12T09:59:00Z", AT)).toBe("queued");
    expect(waitingWord("2026-09-13T05:12:00Z", AT)).toBe("scheduled");
    // A chat send is admitted with `due = now`, so one rule serves both kinds of
    // entry and a calendar one crosses between the words at its own due time
    // with nothing about it changing.
    expect(waitingWord(new Date(AT).toISOString(), AT)).toBe("queued");
  });

  it("reads an unparseable stamp as queued rather than as the far future", () => {
    // The wrong direction to be wrong in is a row claiming it will run on a date
    // nobody can make sense of.
    expect(waitingWord("", AT)).toBe("queued");
    expect(waitingWord("not a date", AT)).toBe("queued");
  });

  it("says WHEN before the due time and WHAT IS IN THE WAY after it", () => {
    // Two different questions, and only one of them has an answer at any moment.
    // 12 September 2026 really is a Saturday — the row spells the weekday out of
    // the stamp, so the assertion is the calendar's and not a fixture's.
    const sat = new Date(2026, 8, 12, 5, 12).toISOString();
    expect(waitingLine({ word: "scheduled", due: sat }, "behind TASK-038"))
      .toEqual(["scheduled", "Sat 12 Sep, 05:12"]);
    expect(waitingLine({ word: "queued", due: "2026-09-12T09:59:00Z" }, "behind TASK-038"))
      .toEqual(["queued", "behind TASK-038"]);
    // …and the word ALONE when the folder is free: no padding half-sentence.
    expect(waitingLine({ word: "queued", due: "2026-09-12T09:59:00Z" }, "")).toEqual(["queued"]);
    expect(waitingLine({ word: "scheduled", due: "nonsense" }, "")).toEqual(["scheduled"]);
  });

  it("spells the time itself rather than asking a locale", () => {
    // The row is one line in a transcript: the format has to be short, the same
    // width every day of the week, and the same string in a test as on screen.
    // `toLocaleString` gives none of the three.
    const d = new Date(2026, 8, 12, 5, 12);
    expect(waitingWhen(d.toISOString())).toBe("Sat 12 Sep, 05:12");
    expect(waitingWhen("")).toBe("");
    expect(waitingWhen("not a date")).toBe("");
  });
});

describe("a seed whose entry ran before any poll saw it", () => {
  it("comes down after a bounded number of poll ANSWERS, not seconds", () => {
    // A folder that frees a second after the admission dispatches the entry
    // before any poll lists it, so its id is never seen pending — and a seed held
    // for the life of the page would be a message claiming to be waiting while
    // its reply streams in above it.
    //
    // Counted in answers because a hidden tab has its timers throttled to about
    // one lap a minute: thirty wall-clock seconds can pass with ZERO answers, and
    // a clock-based bound then expires a row on evidence nobody gathered.
    const seeds = [seed("a")];
    let watch = EMPTY_SEED_WATCH;
    for (let lap = 1; lap <= WAITING_UNSEEN_POLLS; lap += 1) {
      const out = reconcileSeeds(seeds, new Set<string>(), watch);
      watch = out.watch;
      if (lap < WAITING_UNSEEN_POLLS) expect(out.live).toHaveLength(1);
      else expect(out.live).toHaveLength(0);
    }
  });

  it("stays up while nothing has polled at all", () => {
    // An empty set from a poller that has never answered would read as "it
    // already went" and take the row down in the same breath as it went up.
    expect(reconcileSeeds([seed("a")], null, EMPTY_SEED_WATCH).live).toHaveLength(1);
  });

  it("is idempotent for one poll — the render and the effect cannot disagree", () => {
    const seeds = [seed("a")];
    const poll = new Set<string>();
    const first = reconcileSeeds(seeds, poll, EMPTY_SEED_WATCH);
    // Same poll identity, the watch the first call returned: the same answer.
    const again = reconcileSeeds(seeds, poll, first.watch);
    expect(again.live).toHaveLength(first.live.length);
    expect(again.watch).toBe(first.watch);
  });

  it("treats an absence as real the moment a poll has once CONFIRMED the entry", () => {
    const seeds = [seed("a")];
    const seen = reconcileSeeds(seeds, new Set(["a"]), EMPTY_SEED_WATCH);
    expect(seen.live).toHaveLength(1);
    // One answer without it is enough now — the entry fired, or the Tasks page
    // cancelled it, and either way the words are no longer waiting.
    expect(reconcileSeeds(seeds, new Set<string>(), seen.watch).live).toHaveLength(0);
  });

  it("forgets a seed the chat no longer holds, rather than logging it forever", () => {
    // The watch is a memory of the rows on screen, not a log of everything ever
    // queued. (An EMPTY seed list takes the cheap path and hands the watch
    // straight back — a chat with nothing waiting is the ordinary state of every
    // chat, and charging it a fresh watch object four times a minute for a fact
    // nobody draws is exactly the cost the dedupe exists to avoid.)
    const seen = reconcileSeeds([seed("a"), seed("b")], new Set(["a", "b"]), EMPTY_SEED_WATCH);
    expect(seen.watch.seen.size).toBe(2);
    const gone = reconcileSeeds([seed("a")], new Set(["a"]), seen.watch);
    expect([...gone.watch.seen]).toEqual(["a"]);
  });
});

describe("a delete is remembered until the poll agrees", () => {
  it("keeps the id while the poll still lists it, and forgets it after", () => {
    const dropped = new Set(["a"]);
    expect(pruneDropped(dropped, new Set(["a"]))).toBe(dropped);
    expect(pruneDropped(dropped, new Set<string>()).size).toBe(0);
  });

  it("keeps everything while nothing has polled — the safe direction", () => {
    // A page that is not polling keeps the dismissal: a row that is not drawn,
    // over an entry that is not there.
    const dropped = new Set(["a"]);
    expect(pruneDropped(dropped, null)).toBe(dropped);
  });

  it("hands back the SAME set when nothing moved", () => {
    // Otherwise a caller holding it in state re-renders four times a minute for a
    // memory that did not change.
    const dropped = new Set(["a", "b"]);
    expect(pruneDropped(dropped, new Set(["a", "b"]))).toBe(dropped);
  });
});

describe("what is in front is ONE answer for the whole chat", () => {
  it("prefers the server's task row over the admission's", () => {
    // A folder is held by one task, so three messages waiting in one line are all
    // behind the same thing — and the row is the server's, which is why a reload
    // says the same sentence the send did.
    const facts = waitingFacts(
      { queue_position: 2, queue_ahead: "TASK-038", queue_ahead_session: "s", queue_ahead_target: "/p" },
      { queue_position: 9, queue_ahead: "TASK-999" },
    );
    expect(facts.queue_ahead).toBe("TASK-038");
    expect(facts.queue_position).toBe(2);
    expect(facts.status).toBe("queued");
  });

  it("falls back to the admission before that row has been read", () => {
    // The first paint after Enter, and the whole life of a chat with no session
    // — which has no `/api/tasks` row at all.
    const facts = waitingFacts(null, { queue_position: 1, queue_ahead: "TASK-041" });
    expect(facts.queue_ahead).toBe("TASK-041");
    expect(facts.queue_position).toBe(1);
  });

  it("answers 'nothing in front' rather than undefined when neither knows", () => {
    const facts = waitingFacts(null, null);
    expect(facts.queue_ahead).toBe("");
    expect(facts.queue_priority).toBe(false);
  });

  it("ignores a task row that carries no queue fields at all", () => {
    // An older server, or this conversation's row read before the queue placed
    // it: an empty row must not erase what the admission just answered.
    const facts = waitingFacts({ key: "k" } as never, { queue_ahead: "TASK-041" });
    expect(facts.queue_ahead).toBe("TASK-041");
  });
});
