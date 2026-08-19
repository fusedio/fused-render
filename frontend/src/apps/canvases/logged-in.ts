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
import { getCanvasesStatus, type CanvasesStatus } from "./api";

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
/**
 * The `creds_stamp` of a credentials store the SERVER has already refused.
 *
 * `/api/canvases/status` answers `logged_in` from the file EXISTING, nothing
 * more — so a store that is present but unrefreshable (the CLI says
 * re-authenticate; the guarded endpoints 401) reads as signed in here forever,
 * and without this the row would come back on the next tick after the page had
 * just replaced itself with the sign-in wall (bugbot, 2026-08-19). The page
 * publishes that refusal; this remembers WHICH store was refused, so the poll
 * can tell "the same dead credentials" from "someone signed in again". It is
 * the stamp and not a boolean because a re-login over a stale-but-present store
 * never flips `logged_in` — the mtime changing is the whole signal, which is
 * why the page's own login poll watches it too.
 */
let deniedStamp: number | null = null;
const listeners = new Set<(v: boolean) => void>();

function set(next: boolean) {
  if (loggedIn === next) return;
  loggedIn = next;
  for (const listener of listeners) listener(next);
}

/**
 * What a bare status read means once a refusal is remembered — the whole rule,
 * pure, so the suite can hold it without a fake clock or a fake server.
 *
 * A store whose stamp is the refused one stays signed out however cheerfully
 * `/api/canvases/status` reports it; anything else is taken at face value,
 * because a NEW stamp is exactly what completing a re-login looks like.
 */
export function decideLoggedIn(
  status: CanvasesStatus,
  denied: number | null,
): boolean {
  if (!status.logged_in) return false;
  return !(status.creds_stamp !== null && status.creds_stamp === denied);
}

async function poll() {
  if (inFlight) return;
  inFlight = true;
  const departed = generation;
  try {
    const status = await getCanvasesStatus();
    if (generation === departed) set(decideLoggedIn(status, deniedStamp));
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

/**
 * Hand over a known-fresh answer — the status the Canvases page's own poll
 * returned, INCLUDING the one it writes when a guarded call comes back 401.
 *
 * That 401 is the only place either side learns that a present credentials
 * store is dead, so it is remembered (see `deniedStamp`) rather than merely
 * applied: this store's own poll cannot re-derive it, and would otherwise undo
 * the page's own verdict a minute later.
 */
export function publishLoggedIn(status: CanvasesStatus) {
  generation += 1;
  if (status.logged_in) deniedStamp = null;
  else if (status.creds_stamp !== null) deniedStamp = status.creds_stamp;
  set(status.logged_in);
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
