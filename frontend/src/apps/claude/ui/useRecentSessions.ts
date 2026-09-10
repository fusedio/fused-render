// The landing page's Recent list, subscribed. `protocol/sessions.ts` owns the
// reads (the `sessions` action plus the `/api/tasks/changes` long-poll, T:18339
// watchRecent / T:18390 loadRecent); this is the React seam.
//
// `null` is the SKELETON state and not "empty": `subscribeRecent` fires `null`
// first, then a row array for every read, so a re-read over a drawn list
// repaints in place rather than blinking its rows into placeholder bars
// (T:18411).
import { useEffect, useRef, useState } from "react";
import { subscribeRecent } from "../protocol/sessions";
import type { SessionRow } from "../protocol/types";

/**
 * The subscription, injectable — and injectable rather than module-mocked for
 * the reason `useSchedule`'s three endpoint calls are: `bun test` runs every
 * suite in ONE process, so a `mock.module("../protocol/sessions", …)` replaces
 * that module for every suite loaded AFTER it. (An ESM namespace object is also
 * frozen, so the patch-and-restore road is not open here at all.)
 */
export type SubscribeRecent = typeof subscribeRecent;

export function useRecentSessions(
  agentDir: string | null,
  file: string | null,
  subscribe: SubscribeRecent = subscribeRecent,
): SessionRow[] | null {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  /** Has this list ever had real rows in it? The skeleton is only honest before
   *  the first answer of the page's life; after that a `null` means "reading
   *  again", which is not the same news. */
  const painted = useRef(false);
  useEffect(() => {
    // ENTERING A CHAT DOES NOT EMPTY THE LIST. T clears `#recentlist`'s markup
    // on the way back but deliberately does NOT reset the counts: "both reads
    // are about to run again and land within a few hundred ms, and clearing
    // them first would take the tab bar off screen and put it back for the trip
    // — a stale count for a moment is quieter than a section that blinks. Boot
    // is the only place the 'not read yet' state is real." (T:13049-13053)
    //
    // So a torn-down subscription leaves the rows exactly where they were, and
    // the tab bar over them does not flash out and back for the round trip.
    if (!agentDir) return;
    return subscribe(agentDir, file, (next) => {
      // THE SKELETON STANDS IN FOR ROWS WE DO NOT HAVE — never for rows that
      // are already up (T:18408-18411). `subscribeRecent` opens every
      // subscription with `null`, so without this a target change (or the
      // re-subscribe on the way back to the landing) blinked drawn rows into
      // placeholder bars, which "would make every retry look like the list lost
      // the chats it is about to reprint".
      if (next === null) {
        if (!painted.current) setRows(null);
        return;
      }
      painted.current = true;
      setRows(next);
    });
    // `subscribe` is deliberately NOT a dependency: a caller that passes a fresh
    // closure every render would re-subscribe on every render, and the identity
    // of the transport is not a fact about the target.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentDir, file]);
  return rows;
}
