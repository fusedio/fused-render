// DOES THIS SERVER OFFER BOTS (and, the same way, the launcher panel), for a
// reader with no `config` prop — the Claude rail mounts from apps and the
// shell alike, and only the shell's top (App, GlobalSidebar) holds the config.
// One `/api/config` read per page, shared; until it answers the answer is NO,
// so a server without Bots (Windows) never draws the rail's Bots door or polls
// `/api/bots/pulse` for a frame, and a server with no launcher panel never
// draws its Search door.
//
// A read that stalls or fails is not the answer: it is bounded (READ_MS) and
// retried (RETRY_MS) for as long as a reader is mounted, and every mounted
// reader hears the first answer that lands.
import { useEffect, useState } from "react";
import { botsAvailable, getConfig, launcherAvailable, type Config } from "./api";

const READ_MS = 10_000;
const RETRY_MS = 15_000;

let known: Config | null = null;
let reading: Promise<Config | null> | null = null;
const listeners = new Set<(c: Config) => void>();
let retry: ReturnType<typeof setTimeout> | null = null;

function settle(c: Config): void {
  known = c;
  for (const l of [...listeners]) l(c);
}

function read(): void {
  if (known !== null || reading) return;
  const ctl = new AbortController();
  const deadline = setTimeout(() => ctl.abort(), READ_MS);
  reading = getConfig({ signal: ctl.signal })
    .catch(() => null)
    .finally(() => clearTimeout(deadline));
  void reading.then((c) => {
    reading = null;
    if (c !== null) {
      settle(c);
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

/** One yes/no off the shared config read. `enabled` false: no read at all
 *  (the flag that would draw the door is off) — the answer is NO until it
 *  turns on. */
function useConfigFlag(pick: (c: Config) => boolean, enabled: boolean): boolean {
  const [on, setOn] = useState<boolean>(known ? pick(known) : false);
  useEffect(() => {
    if (!enabled) return;
    if (known !== null) {
      setOn(pick(known));
      return;
    }
    const hear = (c: Config) => setOn(pick(c));
    listeners.add(hear);
    read();
    return () => {
      listeners.delete(hear);
    };
  }, [enabled, pick]);
  return enabled && on;
}

export function useBotsAvailable(enabled = true): boolean {
  return useConfigFlag(botsAvailable, enabled);
}

/** Does this process have the launcher panel to show (`/api/config`
 *  `launcher_available`; the rail's Search door)? */
export function useLauncherAvailable(enabled = true): boolean {
  return useConfigFlag(launcherAvailable, enabled);
}

function forget(): void {
  reading = null;
  if (retry) clearTimeout(retry);
  retry = null;
  listeners.clear();
}

/** Tests: pin the Bots answer (`null` forgets the config, so the next mount
 *  reads again). Keeps whatever the launcher answer was. */
export function setBotsAvailableForTests(v: boolean | null): void {
  known = v === null ? null : ({ ...(known ?? {}), bots_available: v } as Config);
  forget();
}

/** Tests: pin the launcher answer, as `setBotsAvailableForTests` does. */
export function setLauncherAvailableForTests(v: boolean | null): void {
  known = v === null ? null : ({ ...(known ?? {}), launcher_available: v } as Config);
  forget();
}
