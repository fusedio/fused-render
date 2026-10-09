// Whether this machine is signed in to Fused, for readers OUTSIDE the Canvases
// page — today the shell sidebar, which shows its Canvases row only once there
// is an account behind it.
//
// A SEPARATE MODULE FROM index.ts, for the reason available.ts spells out for
// Claude Config: the barrel re-exports Canvases/CanvasWorkspace, so a sidebar
// that probed through it would pull the whole canvases app — workspace, lock
// lib, embed host — into the shell's main bundle for one boolean. Import this
// file directly and the app itself stays lazy behind its route.
//
// ONE SUBSCRIPTION, TWO READERS (shell/tasksPulse's shape, same reason): the
// sidebar subscribes, and the Canvases page PUBLISHES what its own status read
// returned — including the fast in-flight read it runs during the browser
// login — so the row appears the moment the login lands instead of waiting for
// the bus to notice. Unlike claude_config's availability this flips
// mid-session: signing in and out is a thing people do, so one cached answer
// would be wrong for the rest of the session.
//
// The module follows `/api/canvases/status` as the `canvases.status` topic of
// the events bus: the server answers a subscribe with a snapshot and pushes a
// fresh one when the credentials store changes (a `fused login` in a terminal,
// another window signing out). The lane opens with the first reader and
// closes with the last — nothing is subscribed on behalf of a sidebar nobody
// has mounted.
import { useEffect, useState } from "react";
import type { CanvasesStatus } from "./api";
import { resyncTopic, subscribeTopic } from "@platform/lib/events";

let loggedIn = false;
/** This module's own subscription to `canvases.status`, or null while the
 *  lane is closed. */
let lane: (() => void) | null = null;
/**
 * The `creds_stamp` of a credentials store the SERVER has already refused.
 *
 * `/api/canvases/status` answers `logged_in` from the file EXISTING, nothing
 * more — so a store that is present but unrefreshable (the CLI says
 * re-authenticate; the guarded endpoints 401) reads as signed in here forever,
 * and without this the row would come back on the next snapshot after the page
 * had just replaced itself with the sign-in wall (bugbot, 2026-08-19). The page
 * publishes that refusal; this remembers WHICH store was refused, so a snapshot
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

/** Open the lane while anyone reads, close it once nobody does. A refused
 *  frame is not a sign-out: the sidebar keeps the row it had rather than
 *  dropping it because one read lost a race with a server restart. */
function schedule() {
  const wanted = listeners.size > 0;
  if (!wanted) {
    if (lane) {
      const stop = lane;
      lane = null;
      stop();
    }
    return;
  }
  if (lane) return;
  lane = subscribeTopic<CanvasesStatus>("canvases.status", {}, (status, _delta, meta) => {
    if (meta.error !== undefined || status === null) return;
    set(decideLoggedIn(status, deniedStamp));
  });
}

/**
 * Hand over a known-fresh answer — the status the Canvases page's own read
 * returned, INCLUDING the one it writes when a guarded call comes back 401.
 *
 * That 401 is the only place either side learns that a present credentials
 * store is dead, so it is remembered (see `deniedStamp`) rather than merely
 * applied: a snapshot cannot re-derive it, and would otherwise undo the page's
 * own verdict the next time the bus pushed one. The bus is then asked for a
 * fresh snapshot — a login or logout this document just drove is a write the
 * producer may take a beat to notice.
 */
export function publishLoggedIn(status: CanvasesStatus) {
  if (status.logged_in) deniedStamp = null;
  else if (status.creds_stamp !== null) deniedStamp = status.creds_stamp;
  set(status.logged_in);
  if (lane) resyncTopic("canvases.status", {});
}

/** Subscribe. The lane opens with the first reader and closes with the last. */
export function useCanvasesLoggedIn(): boolean {
  const [current, setCurrent] = useState(loggedIn);
  useEffect(() => {
    listeners.add(setCurrent);
    // `loggedIn` already holds the last answer, so a sidebar remounting on
    // every navigation (App keys it on the nav epoch) never blinks the row;
    // the client replays the cached snapshot to a late reader at once.
    setCurrent(loggedIn);
    schedule();
    return () => {
      listeners.delete(setCurrent);
      schedule();
    };
  }, []);
  return current;
}
