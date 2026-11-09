// The working line's clock: when the CURRENT turn started.

/** A server stamp older than this is a stale one, not this turn's start. */
const MAX_STAMP_AGE_MS = 24 * 3600 * 1000;

export interface TurnClock {
  /** Epoch ms the line counts from. */
  startedAt: number;
  /** The server stamp `startedAt` last followed (ms), 0 before the first. */
  stamp: number;
}

/**
 * The clock after a poll that carried `serverStartMs` (the server's stamp for
 * the turn now in flight; 0 = unknown). The first stamp seen only ever moves
 * the clock earlier (a reload, another tab and a reopened pane all keep
 * counting from the real start). A stamp that DIFFERS from the one already
 * followed is a new turn on the same run, and the clock restarts at it.
 */
export function advanceTurnClock(clock: TurnClock, serverStartMs: number, nowMs: number): TurnClock {
  if (!(serverStartMs > 0) || nowMs - serverStartMs > MAX_STAMP_AGE_MS) return clock;
  const at = Math.min(serverStartMs, nowMs);
  if (clock.stamp > 0 && clock.stamp !== serverStartMs) return { startedAt: at, stamp: serverStartMs };
  const startedAt = !(clock.startedAt > 0) || at < clock.startedAt ? at : clock.startedAt;
  return { startedAt, stamp: serverStartMs };
}
