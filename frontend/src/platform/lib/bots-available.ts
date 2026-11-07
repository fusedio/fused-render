// DOES THIS SERVER OFFER BOTS, for a reader with no `config` prop — the
// Claude rail mounts from apps and the shell alike, and only the shell's top
// (App, GlobalSidebar) holds the config. One `/api/config` read per page,
// shared; until it answers the answer is NO, so a server without Bots (Windows)
// never draws the rail's Bots door or polls `/api/bots/pulse` for a frame.
//
// A read that stalls or fails is not the answer: it is bounded (READ_MS) and
// retried (RETRY_MS) for as long as a reader is mounted, and every mounted
// reader hears the first answer that lands.
import { useEffect, useState } from "react";
import { botsAvailable, getConfig } from "./api";

const READ_MS = 10_000;
const RETRY_MS = 15_000;

let known: boolean | null = null;
let reading: Promise<boolean | null> | null = null;
const listeners = new Set<(v: boolean) => void>();
let retry: ReturnType<typeof setTimeout> | null = null;

function settle(v: boolean): void {
  known = v;
  for (const l of [...listeners]) l(v);
}

function read(): void {
  if (known !== null || reading) return;
  const ctl = new AbortController();
  const deadline = setTimeout(() => ctl.abort(), READ_MS);
  reading = getConfig({ signal: ctl.signal })
    .then((c) => botsAvailable(c))
    .catch(() => null)
    .finally(() => clearTimeout(deadline));
  void reading.then((v) => {
    reading = null;
    if (v !== null) {
      settle(v);
      return;
    }
    // No answer: try again while somebody is still asking.
    if (listeners.size > 0 && !retry) {
      retry = setTimeout(() => {
        retry = null;
        if (listeners.size > 0) read();
      }, RETRY_MS);
    }
  });
}

/** `enabled` false: no read at all (the flag that would draw a Bots door or
 *  poll the pulse is off) — the answer is NO until it turns on. */
export function useBotsAvailable(enabled = true): boolean {
  const [on, setOn] = useState<boolean>(known ?? false);
  useEffect(() => {
    if (!enabled) return;
    if (known !== null) {
      setOn(known);
      return;
    }
    listeners.add(setOn);
    read();
    return () => {
      listeners.delete(setOn);
    };
  }, [enabled]);
  return enabled && on;
}

/** Tests: pin the answer (`null` forgets it, so the next mount reads again). */
export function setBotsAvailableForTests(v: boolean | null): void {
  known = v;
  reading = null;
  if (retry) clearTimeout(retry);
  retry = null;
  listeners.clear();
}
