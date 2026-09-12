// THE CHIP THAT VANISHED ON THE KEYSTROKE THAT PUT IT UP — and, round two, the
// chip that vanished on a clock nobody was reading.
//
// `pendingIds` is a photograph up to a poll interval old, and the admission that
// created the entry happened after it — so the first filter of a just-queued
// send against that set answered "not pending" and took the chip straight back
// down. The grace that fixes it used to be thirty wall-clock seconds, which is
// two laps only of a page that is actually polling: a hidden tab's timers are
// throttled, so thirty seconds could pass with no answers at all and the chip
// then came down on evidence nobody had gathered. The bound is counted in poll
// ANSWERS now, and these are the states that rule has to give.
import { describe, expect, it } from "bun:test";
import {
  EMPTY_QUEUED_WATCH,
  NO_DISMISSED,
  QUEUED_UNSEEN_POLLS,
  pruneDismissed,
  reconcileQueuedSends,
  type QueuedWatch,
} from "./queued-sends";

const send = (entryId: string) => ({ entryId });
const ids = (list: string[]) => new Set(list);
const fresh = (): QueuedWatch => EMPTY_QUEUED_WATCH;

describe("reconcileQueuedSends", () => {
  it("keeps every chip while nobody has polled", () => {
    // `null` is "not asked yet" and always was: an empty set there would read as
    // "they all went".
    const sends = [send("e1"), send("e2")];
    const out = reconcileQueuedSends(sends, null, fresh());
    expect(out.live.map((s) => s.entryId)).toEqual(["e1", "e2"]);
    expect(out.watch).toBe(EMPTY_QUEUED_WATCH);
  });

  it("keeps a send the last poll is simply older than", () => {
    // THE BUG THIS FILE EXISTS FOR. The poll ran, then the reader pressed Enter,
    // then the server made the entry — so the set in hand cannot possibly name
    // it, and the chip must not read that as "your message already went".
    const out = reconcileQueuedSends([send("e1")], ids(["other"]), fresh());
    expect(out.live.map((s) => s.entryId)).toEqual(["e1"]);
    // …and it is still not SEEN, so the next answer is judged the same way.
    expect([...out.watch.seen]).toEqual([]);
    expect(out.watch.missed.get("e1")).toBe(1);
  });

  it("remembers an id the poll has once listed", () => {
    const out = reconcileQueuedSends([send("e1")], ids(["e1"]), fresh());
    expect(out.live.map((s) => s.entryId)).toEqual(["e1"]);
    expect([...out.watch.seen]).toEqual(["e1"]);
  });

  it("takes the chip down only once a poll that HAD it no longer does", () => {
    // The entry fired, or the Tasks page cancelled it. Either way the words are
    // no longer waiting and the chip has nothing left to say.
    const sends = [send("e1")];
    const watch = reconcileQueuedSends(sends, ids(["e1"]), fresh()).watch;
    const gone = reconcileQueuedSends(sends, ids([]), watch);
    expect(gone.live).toEqual([]);
  });

  it("gives up on a send no poll ever saw, after N ANSWERS — not after N seconds", () => {
    // A folder that frees a second after the admission dispatches the entry
    // before any poll lists it. Without a bound that chip would sit there for
    // the life of the page, claiming a message is waiting while its reply
    // streams in above it. The bound is spent by ANSWERS, so a page nobody is
    // answering never spends it.
    const sends = [send("e1")];
    let watch = fresh();
    for (let i = 1; i < QUEUED_UNSEEN_POLLS; i++) {
      const out = reconcileQueuedSends(sends, ids(["other" + i]), watch);
      expect(out.live.map((s) => s.entryId)).toEqual(["e1"]);
      watch = out.watch;
    }
    const after = reconcileQueuedSends(sends, ids(["last"]), watch);
    expect(after.live).toEqual([]);
  });

  it("spends nothing on a re-render that is not a new poll", () => {
    // The composer stays open, so a SECOND send re-runs this rule against the
    // pending set already in hand. Charging that set twice would halve the grace
    // of every chip on screen.
    const poll = ids(["other"]);
    const first = reconcileQueuedSends([send("e1")], poll, fresh());
    const again = reconcileQueuedSends([send("e1"), send("e2")], poll, first.watch);
    expect(again.live.map((s) => s.entryId)).toEqual(["e1", "e2"]);
    expect(again.watch.missed.get("e1")).toBe(1);
    // …and the same poll, asked a third time, still says the same thing.
    const third = reconcileQueuedSends([send("e1"), send("e2")], poll, again.watch);
    expect(third.live.map((s) => s.entryId)).toEqual(["e1", "e2"]);
  });

  it("does not expire a chip across a stretch with no answers at all", () => {
    // THE ROUND-2 FINDING, in one test: the tab was hidden, its timers were
    // throttled, and by wall clock the old bound was long gone. Nothing asked
    // the server, so nothing is known, so the chip stays.
    const sends = [send("e1")];
    const out = reconcileQueuedSends(sends, null, fresh());
    expect(out.live.map((s) => s.entryId)).toEqual(["e1"]);
    // …and the first answer to arrive after the tab comes back still finds it
    // pending, which is the case the wall clock was dropping.
    const back = reconcileQueuedSends(sends, ids(["e1"]), out.watch);
    expect(back.live.map((s) => s.entryId)).toEqual(["e1"]);
  });

  it("judges each send on its own count, not the list's", () => {
    // The composer stays open under the queue, so several messages can be in one
    // folder's line, admitted at different laps.
    const out1 = reconcileQueuedSends([send("old")], ids(["x"]), fresh());
    expect(out1.live.map((s) => s.entryId)).toEqual(["old"]);
    // A second send joins, and the next answer misses both: the older one has
    // now been missed twice, the newer one once.
    const out2 = reconcileQueuedSends([send("old"), send("new")], ids(["y"]), out1.watch);
    expect(out2.live.map((s) => s.entryId)).toEqual(["new"]);
    expect(out2.watch.missed.get("new")).toBe(1);
  });

  it("hands back the SAME watch when nothing moved", () => {
    // The caller holds this in state; a fresh object every poll would be a
    // re-render every fifteen seconds for a fact that did not change.
    const poll = ids(["e1"]);
    const first = reconcileQueuedSends([send("e1")], poll, fresh());
    const again = reconcileQueuedSends([send("e1")], poll, first.watch);
    expect(again.watch).toBe(first.watch);
  });

  it("costs a chat with no chips nothing at all", () => {
    // Every chat is in this state almost all of the time, and the poll answers
    // four times a minute.
    const watch = fresh();
    const out = reconcileQueuedSends([], ids(["someone-else"]), watch);
    expect(out.live).toEqual([]);
    expect(out.watch).toBe(watch);
  });

  it("forgets the ids of sends it no longer holds", () => {
    // The watch is a memory of the chips on screen, not a log of everything ever
    // queued.
    const watch = reconcileQueuedSends([send("e1")], ids(["e1"]), fresh()).watch;
    expect([...watch.seen]).toEqual(["e1"]);
    const out = reconcileQueuedSends([send("e2")], ids(["e2"]), watch);
    expect([...out.watch.seen]).toEqual(["e2"]);
  });

  it("seen ids do not leak across sends — only the one that was listed", () => {
    const sends = [send("e1"), send("e2")];
    const out = reconcileQueuedSends(sends, ids(["e1"]), fresh());
    expect([...out.watch.seen]).toEqual(["e1"]);
    // e2 is unseen and has been missed once: still up, and on its own terms.
    expect(out.live.map((s) => s.entryId)).toEqual(["e1", "e2"]);
  });
});

// THE OTHER HALF OF THE SAME PHOTOGRAPH. A cancel pressed on a chip drops the
// send on the server's answer, which also drops its id out of the block's
// filter (`chipEntryIds`) — while `blockers`, taken from a poll up to a lap
// older than the press, still lists the entry. The block therefore came back up
// over the message the reader had just cancelled (Bugbot, PR #1124 round 2).
describe("pruneDismissed", () => {
  it("keeps a dismissal while nobody has polled", () => {
    const held = new Set(["e1"]);
    expect(pruneDismissed(held, null)).toBe(held);
  });

  it("keeps it across a poll that STILL lists the entry — the stale one", () => {
    // The answer in flight when Cancel was pressed. It has the entry, so the
    // block has it too, and the dismissal is the only thing keeping the card
    // down.
    const held = new Set(["e1"]);
    expect(pruneDismissed(held, ids(["e1", "e2"]))).toBe(held);
  });

  it("forgets it on the first poll that omits the entry", () => {
    // `pendingIds` and `blockers` are published by the same tick, so this is
    // exactly the poll after which the block cannot draw it either.
    const out = pruneDismissed(new Set(["e1", "e2"]), ids(["e2"]));
    expect([...out]).toEqual(["e2"]);
  });

  it("costs nothing when there is nothing to remember", () => {
    expect(pruneDismissed(NO_DISMISSED, ids(["e1"]))).toBe(NO_DISMISSED);
  });

  it("never expires on a clock — a page with no answers keeps every dismissal", () => {
    // The same rule the chips' own grace is counted by: nothing here reads
    // `Date.now()`, so a throttled tab holds its dismissals rather than
    // unmasking a block on evidence nobody gathered.
    let held: ReadonlySet<string> = new Set(["e1"]);
    for (let i = 0; i < 10; i++) held = pruneDismissed(held, null);
    expect([...held]).toEqual(["e1"]);
  });
});
