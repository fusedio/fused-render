// WHEN A QUEUED SEND'S CHIP COMES DOWN — the one question the chat cannot
// answer by looking at `pendingIds` alone.
//
// The chip under a queued bubble says "these words are waiting in this folder's
// line", so it has to leave the moment the words go: the entry fired, or the
// Tasks page cancelled it. Both read as "that id is no longer pending", which is
// exactly what the schedule poll's unfiltered pending set says (useSchedule
// `pendingIds`, scheduled.ts `onPending`).
//
// WHAT THAT SET CANNOT SAY IS "NOT YET". It is a photograph taken up to a poll
// interval ago, and the admission that created the entry happened AFTER it —
// so filtering a just-admitted send against it drops the chip in the same
// breath as it goes up, on the one gesture the reader is looking straight at.
// A `null` (nobody has polled) was already handled; a stale non-null was not.
//
// SO AN ID IS UNSEEN UNTIL A POLL HAS ONCE CONTAINED IT, and only from then on
// does its absence mean anything. That is the whole state machine, and it is
// kept here — pure, and beside the poller whose facts it reads — rather than
// inline in a `useMemo`, because "a chip vanished" is not something a render
// test of a 3000-line view would ever have caught.
import { useEffect, useMemo, useState } from "react";

/**
 * How many poll answers may miss a send before its chip comes down.
 *
 * THE BOUND EXISTS BECAUSE "UNSEEN" CAN BE PERMANENT. A folder that frees a
 * second after the admission dispatches the entry before any poll lists it, so
 * the id is never seen pending and the rule above would hold that chip for the
 * life of the page — a message claiming to be waiting while its reply streams in
 * above it. Two laps is long enough to cover a tick that was already in flight
 * when the send was admitted, which is the race this whole file is about.
 *
 * POLLS, NOT WALL CLOCK, and that was the round-2 finding. The bound used to be
 * 30 seconds of `Date.now()`, on the arithmetic that two 15 s laps take thirty
 * seconds — which is only true of a page that is actually polling. Nothing
 * guarantees that: a hidden tab has its timers throttled to about one lap a
 * minute, and a chat embedded in a pane the host has stopped painting can get
 * fewer still. So thirty wall-clock seconds can pass with ZERO answers from the
 * server, and the old bound then expired a chip on evidence nobody had gathered
 * — taking a still-queued message's chip down at the very moment the reader
 * looked back at the tab. Counting poll ANSWERS is the same intent measured on
 * the clock that matters, and `onPending` fires on successful ticks only
 * (scheduled.ts), so every lap counted here is a real answer about a real
 * schedule.
 */
export const QUEUED_UNSEEN_POLLS = 2;

/** The half of a queued send this reconciliation reads: which entry it is.
 *  Structural, so the chat's own `QueuedSend` (which also carries the caption
 *  facts) satisfies it. */
export interface QueuedLike {
  entryId: string;
}

/**
 * What the chat remembers BETWEEN polls, and the whole of it.
 *
 * `poll` is the identity of the pending set the counts were last taken against
 * — `absorbPending` publishes a fresh `Set` on every successful tick, so a
 * change of identity IS a lap. It is what makes the counting idempotent: a
 * re-render for some other reason (a second send admitted, a caption updated)
 * re-runs this rule against the same poll, and must not charge that poll twice.
 */
export interface QueuedWatch {
  /** Entry ids a poll has confirmed pending at least once — from then on their
   *  absence is real. */
  seen: ReadonlySet<string>;
  /** Per still-unseen id, how many poll answers have now missed it. */
  missed: ReadonlyMap<string, number>;
  /** The pending set those counts were taken against. */
  poll: ReadonlySet<string> | null;
}

export const EMPTY_QUEUED_WATCH: QueuedWatch = {
  seen: new Set<string>(),
  missed: new Map<string, number>(),
  poll: null,
};

export interface QueuedLiveness<T extends QueuedLike> {
  /** The sends that still have something to say. */
  live: T[];
  /** The memory to carry to the next poll. Returned unchanged (the same object)
   *  when nothing moved, so a caller holding it in state does not re-render for
   *  a no-op. */
  watch: QueuedWatch;
}

/**
 * Which queued sends still deserve a chip.
 *
 *   * `pendingIds === null` — nothing has polled yet: every chip stays, and
 *     nothing is counted.
 *   * in the set — waiting, and now SEEN: its absence from here on is real.
 *   * not in the set, never seen, fewer than `QUEUED_UNSEEN_POLLS` answers have
 *     missed it — the poll is simply older than the send. Keep it.
 *   * not in the set, and either seen before or missed by that many answers —
 *     gone.
 *
 * IDEMPOTENT FOR ONE POLL: called again with the same `pendingIds` and the watch
 * the first call returned, it answers the same thing. That is what lets the
 * render compute `live` and the effect store `watch` without the two ever
 * disagreeing about a chip.
 */
export function reconcileQueuedSends<T extends QueuedLike>(
  sends: readonly T[],
  pendingIds: ReadonlySet<string> | null,
  watch: QueuedWatch,
): QueuedLiveness<T> {
  // NOTHING POLLED, OR NOTHING HELD — and the watch is handed straight back. A
  // chat with no chips up is the ordinary state of every chat, and charging it a
  // new watch object (and so a re-render) four times a minute for a fact nobody
  // is drawing is exactly the cost `absorb`'s own dedupe exists to avoid.
  if (!pendingIds || !sends.length) return { live: sends.slice(), watch };
  const fresh = pendingIds !== watch.poll;
  const seen = new Set(watch.seen);
  const missed = new Map(watch.missed);
  const live: T[] = [];
  for (const send of sends) {
    const id = send.entryId;
    if (pendingIds.has(id)) {
      seen.add(id);
      missed.delete(id);
      live.push(send);
      continue;
    }
    if (seen.has(id)) continue;
    const n = (missed.get(id) ?? 0) + (fresh ? 1 : 0);
    missed.set(id, n);
    if (n < QUEUED_UNSEEN_POLLS) live.push(send);
  }
  // …and the ids of sends this chat no longer holds go with them: the watch is a
  // memory of the chips on screen, not a log of everything ever queued.
  const held = new Set(sends.map((s) => s.entryId));
  for (const id of [...seen]) if (!held.has(id)) seen.delete(id);
  for (const id of [...missed.keys()]) if (!held.has(id)) missed.delete(id);
  const same =
    watch.poll === pendingIds && sameSet(seen, watch.seen) && sameMap(missed, watch.missed);
  return { live, watch: same ? watch : { seen, missed, poll: pendingIds } };
}

function sameSet(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  if (a.size !== b.size) return false;
  for (const v of a) if (!b.has(v)) return false;
  return true;
}

function sameMap(a: ReadonlyMap<string, number>, b: ReadonlyMap<string, number>): boolean {
  if (a.size !== b.size) return false;
  for (const [k, v] of a) if (b.get(k) !== v) return false;
  return true;
}

/**
 * The same rule, as the chat holds it: the watch in state, grown by an effect.
 *
 * GROWN IN AN EFFECT AND NOT DURING THE RENDER, because a render that also
 * writes the memory it read is a render whose answer depends on how many times
 * React ran it. The render's own call already charges the current poll
 * (`fresh`), so the two agree about every chip before the effect has stored
 * anything — the effect is never a paint behind the thing it is remembering.
 *
 * NO TIMER. The bound is counted in poll answers and the poller publishes a
 * fresh `pendingIds` on every successful tick, so the grace is re-judged exactly
 * once per lap and nothing else waits on it.
 */
export function useQueuedLiveness<T extends QueuedLike>(
  sends: readonly T[],
  pendingIds: ReadonlySet<string> | null,
): T[] {
  const [watch, setWatch] = useState<QueuedWatch>(EMPTY_QUEUED_WATCH);
  useEffect(() => {
    setWatch((cur) => reconcileQueuedSends(sends, pendingIds, cur).watch);
  }, [sends, pendingIds]);
  return useMemo(
    () => reconcileQueuedSends(sends, pendingIds, watch).live,
    [sends, pendingIds, watch],
  );
}

// ── THE ENTRIES A CHIP TOOK BACK ─────────────────────────────────────────────
//
// A cancel pressed on a chip drops the send from `queuedSends` on the SERVER'S
// ANSWER rather than on the next poll, because a card that stayed up for fifteen
// seconds after a successful cancel reads as a button that did nothing. That
// removal has a second effect nobody asked for: `chipEntryIds` is derived from
// the very same list, so the id leaves the block's filter in the same paint —
// and the schedule poll's `allBlockers` is a photograph taken up to a poll
// interval ago, which still lists the entry. The block therefore POPPED BACK UP
// over the message the reader had just cancelled ("Blocked — a scheduled message
// runs in this chat … Cancel this message"), and stood there until a poll
// answered (Bugbot, PR #1124 round 2).
//
// So a cancelled id is remembered, and it keeps filtering the block after its
// chip has gone. THE SAME SEEN/UNSEEN ARGUMENT AS ABOVE, read the other way
// round: the absence of an id from a poll only means something once a poll has
// spoken at all, and `pendingIds` and `allBlockers` are published by the same
// tick (`absorb` / `absorbPending`) — so "this poll no longer lists it" is
// exactly the moment the block has stopped holding it too, and the memory can
// go. Nothing expires on a clock: a page that is not polling keeps the
// dismissal, which is the safe direction (a block that is not drawn, over an
// entry that is not there).

export const NO_DISMISSED: ReadonlySet<string> = new Set<string>();

/**
 * The dismissed ids still worth remembering, given the last poll's pending set.
 *
 * `null` (nobody has polled) keeps every id. Otherwise an id the poll no longer
 * lists is dropped — the server agrees the entry is gone, so the block cannot
 * draw it and the filter has nothing left to do.
 *
 * Returns the SAME SET when nothing moved, so a caller holding it in state does
 * not re-render four times a minute for a memory that did not change.
 */
export function pruneDismissed(
  dismissed: ReadonlySet<string>,
  pendingIds: ReadonlySet<string> | null,
): ReadonlySet<string> {
  if (!pendingIds || !dismissed.size) return dismissed;
  let dropped = false;
  const next = new Set<string>();
  for (const id of dismissed) {
    if (pendingIds.has(id)) next.add(id);
    else dropped = true;
  }
  return dropped ? next : dismissed;
}
