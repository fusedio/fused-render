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

/** THE LAST ANSWER, REMEMBERED ACROSS A RELOAD (owner, 2026-11-09: "things
 *  flash … everything should show up instantly"): until the read answered,
 *  the rail drew no Bots or Search door and then popped them in. The two
 *  flags' last answer is kept in localStorage and read as the starting
 *  point; the read corrects it. Only the two booleans are kept. */
const MEMO_KEY = "fused-render:config-doors";
type Doors = Pick<Config, "bots_available" | "launcher_available">;
function memoRead(): Doors | null {
  try {
    const v = JSON.parse(localStorage.getItem(MEMO_KEY) ?? "null") as unknown;
    return v && typeof v === "object" ? (v as Doors) : null;
  } catch {
    return null;
  }
}
function memoWrite(c: Config): void {
  try {
    localStorage.setItem(
      MEMO_KEY,
      JSON.stringify({ bots_available: c.bots_available, launcher_available: c.launcher_available }),
    );
  } catch {
    /* no storage: nothing remembered */
  }
}
const remembered = memoRead();

let known: Config | null = null;
/** What the server said, apart from any test pin laid over it (`known` is the
 *  two together). */
let server: Config | null = null;
let reading: Promise<Config | null> | null = null;
const listeners = new Set<(c: Config) => void>();
let retry: ReturnType<typeof setTimeout> | null = null;

function settle(c: Config): void {
  memoWrite(c);
  server = c;
  known = { ...c, ...pins };
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
  const [on, setOn] = useState<boolean>(() =>
    known ? pick(known) : remembered && Object.keys(pins).length === 0 ? pick(remembered as Config) : false,
  );
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

/** Tests: pin one flag's answer over whatever the read says (`pins`), leaving
 *  the other's alone; `null` lets that flag go — and once neither is pinned
 *  the read's config is forgotten too, so the next mount reads again. */
let pins: Partial<Config> = {};
function pinForTests(key: "bots_available" | "launcher_available", v: boolean | null): void {
  if (v === null) delete pins[key];
  else pins[key] = v;
  // The pins over the server's own answer, never over an earlier pin: a flag
  // let go reads the server's value again (CodeRabbit on #2201).
  known = Object.keys(pins).length > 0 ? ({ ...(server ?? {}), ...pins } as Config) : server;
  forget();
}

export function setBotsAvailableForTests(v: boolean | null): void {
  pinForTests("bots_available", v);
}

export function setLauncherAvailableForTests(v: boolean | null): void {
  pinForTests("launcher_available", v);
}
