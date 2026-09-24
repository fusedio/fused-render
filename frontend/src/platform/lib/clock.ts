// Two facts about time passing, as hooks, because every surface that draws a
// relative stamp or covers a pane with a skeleton needs one of them and every
// one of them had been spelling it out again locally.
//
// `useNow` is the CLOCK: a row that says "2m ago" is a sentence about the gap
// between a stamp and now, and only one of those two moves. Nothing repainted
// it, so a chat left open for an hour still said "2m ago" and the Tasks page's
// lanes still called a run that finished forty minutes ago Upcoming, until the
// next poll happened to land.
//
// `useFallbackAfter` is the BACKSTOP: "this wait has gone on too long to still
// be a wait". Lifted from `platform/ui/ChatFrame.tsx`, which held a timer over
// its own skeleton for as long as the framed chat existed (CHAT_FRAME_FALLBACK_MS;
// the frame itself is gone, D890) — the same shape, wanted in three more places,
// and a covered pane that never uncovers is the worst failure any of them has.
import { useEffect, useState } from "react";

/**
 * ONE TIMER PER INTERVAL, FOR THE WHOLE DOCUMENT — not one per row.
 *
 * A list of forty tasks would otherwise carry forty timers, and, worse, forty
 * timers that fire at forty different instants: two rows a second apart in
 * mounting would flip "59m ago" to "1h ago" a second apart, which is exactly
 * the sort of disagreement between two cells in one column that the row model
 * exists to prevent. Sharing the ticker makes every reader of the same cadence
 * read the SAME instant, so a list re-letters in one paint.
 */
const tickers = new Map<
  number,
  { now: number; timer: ReturnType<typeof setInterval>; subs: Set<(n: number) => void> }
>();

function join(intervalMs: number, sub: (n: number) => void): () => void {
  let t = tickers.get(intervalMs);
  if (!t) {
    const made = {
      now: Date.now(),
      subs: new Set<(n: number) => void>(),
      timer: setInterval(() => {
        const live = tickers.get(intervalMs);
        if (!live) return;
        live.now = Date.now();
        for (const fn of live.subs) fn(live.now);
      }, intervalMs),
    };
    tickers.set(intervalMs, made);
    t = made;
  }
  t.subs.add(sub);
  return () => {
    const live = tickers.get(intervalMs);
    if (!live) return;
    live.subs.delete(sub);
    // The last reader takes the timer with it: a ticker running for nobody is
    // a wake-up a minute for the life of the page.
    if (live.subs.size === 0) {
      clearInterval(live.timer);
      tickers.delete(intervalMs);
    }
  };
}

/**
 * The current instant, re-read every `intervalMs` — for anything whose OUTPUT
 * moves with the clock rather than with the data: a relative stamp, and the
 * lane a task sorts into (`tasks-lib.groupByColumn` asks "is this scheduled for
 * later", which stops being true without anything changing).
 *
 * A MINUTE by default, which is the resolution of everything this repo prints:
 * `relativeWhen`/`timeAgo` say "just now" under a minute and whole minutes
 * above it, so a faster tick would repaint the same words.
 */
export function useNow(intervalMs: number = 60_000): number {
  const [now, setNow] = useState(() => tickers.get(intervalMs)?.now ?? Date.now());
  useEffect(() => {
    // Read once on the way in as well: a component mounting 55 seconds into a
    // shared ticker's minute would otherwise print a stamp up to a minute stale
    // until the next fire.
    setNow(Date.now());
    return join(intervalMs, setNow);
  }, [intervalMs]);
  return now;
}

/** The wait every gate in this app is allowed before it must show something.
 *  The same 8 s `ChatFrame` gave a booting frame before it was deleted (D890),
 *  and the same constant, so the covers over one pane cannot come off at two
 *  times. */
export const GATE_FALLBACK_MS = 8000;

/**
 * `false`, then `true` once `ms` has passed — while `armed`.
 *
 * The shape of a backstop and not of a loading flag: it answers "has this gone
 * on too long", so a caller draws its real content while it is false and its
 * fallback when it turns true, and un-arming (the thing being waited for
 * arrived) puts it back to false rather than leaving a stale verdict up.
 */
export function useFallbackAfter(ms: number, armed: boolean = true): boolean {
  const [late, setLate] = useState(false);
  useEffect(() => {
    if (!armed) {
      setLate(false);
      return;
    }
    const t = setTimeout(() => setLate(true), ms);
    return () => clearTimeout(t);
  }, [ms, armed]);
  return armed && late;
}
