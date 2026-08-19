// Whether this machine is signed in to Fused, for readers OUTSIDE the Canvases
// page — today the shell sidebar, which shows its Workbench canvases row only
// once there is an account behind it.
//
// A SEPARATE MODULE FROM index.ts, for the reason available.ts spells out for
// Claude Config: the barrel re-exports Canvases/CanvasWorkspace, so a sidebar
// that probed through it would pull the whole canvases app — workspace, lock
// lib, embed host — into the shell's main bundle for one boolean. Import this
// file directly and the app itself stays lazy behind its route.
//
// ONE POLL, TWO READERS (shell/tasksPulse's shape, same reason): the sidebar
// subscribes, and the Canvases page PUBLISHES what its own status poll returned
// — including the fast in-flight poll it runs during the browser login — so the
// row appears the moment the login lands instead of waiting out the interval
// below. Unlike claude_config's availability this flips mid-session: signing in
// and out is a thing people do, so one cached answer would be wrong for the
// rest of the session.
import { useEffect, useState } from "react";
import { getCanvasesStatus } from "./api";

// Nothing here is urgent — the poll exists to catch a login that happened
// somewhere else (another window, a `fused login` in a terminal). A login
// started from the page publishes; it never waits for this.
const POLL_MS = 60_000;

let loggedIn = false;
let timer: number | null = null;
let inFlight = false;
/** Which answer is newest: a publish bumps it, and a self-poll that departed
 *  earlier drops its result rather than overwriting the fresher one. */
let generation = 0;
const listeners = new Set<(v: boolean) => void>();

function set(next: boolean) {
  if (loggedIn === next) return;
  loggedIn = next;
  for (const listener of listeners) listener(next);
}

async function poll() {
  if (inFlight) return;
  inFlight = true;
  const departed = generation;
  try {
    const status = await getCanvasesStatus();
    if (generation === departed) set(status.logged_in);
  } catch {
    // A failed read is not a sign-out: the sidebar keeps the row it had rather
    // than dropping it because one poll lost a race with a server restart.
  } finally {
    inFlight = false;
    schedule();
  }
}

function schedule() {
  if (timer !== null) window.clearTimeout(timer);
  timer = null;
  if (listeners.size === 0) return;
  timer = window.setTimeout(poll, POLL_MS);
}

/** Hand over a known-fresh answer — what the Canvases page's own poll returned. */
export function publishLoggedIn(next: boolean) {
  generation += 1;
  set(next);
  schedule();
}

/** Subscribe. Polling starts with the first reader and stops with the last. */
export function useCanvasesLoggedIn(): boolean {
  const [current, setCurrent] = useState(loggedIn);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(loggedIn);
    // Read immediately: the sidebar remounts on every navigation (App keys it
    // on the nav epoch), and `loggedIn` already holds the last answer, so this
    // costs one cheap local status read per trip and never blinks the row.
    void poll();
    return () => {
      listeners.delete(setCurrent);
      schedule();
    };
  }, []);
  return current;
}
