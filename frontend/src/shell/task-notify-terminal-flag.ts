// Whether a finished-task notification also fires for a session that
// entered from an INTERACTIVE TERMINAL, rather than only one started through
// fused-render's own Claude template. The `task_notify_terminal_sessions`
// pref (fused_render/shell/prefs.py), default OFF — the reported bug was a
// plain `claude` session in a terminal raising a fused-render notification
// unasked, so opting BACK into that is what this flag gates, not the other
// way around.
//
// A CLONE OF `task-peek-flag.ts`, deliberately down to the shape: one
// shared GET, a generation guard so a publish beats a slower in-flight read,
// and `null` meaning "not asked yet". See that file's own header for why the
// tri-state matters — the same reasoning applies here: a premature `true`
// would notify for a terminal session and then, once the real (off) answer
// lands, produce nothing for the next one — an inconsistency the reader
// would read as a bug, not a race.
//
// THE GATE ITSELF lives in task-status-notify.ts, not here — this module
// only answers "is the preference on", the same narrow job every other flag
// module in this directory does.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";

let enabled: boolean | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean | null) => void>();

function set(next: boolean | null) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefs()
    // One bounded retry, then a real answer either way — a prefs GET that fails
    // is usually a single dropped request (a reload racing the server's start).
    .catch(() => getPrefs())
    .then((p) => {
      if (generation !== departed) return;
      // `=== true` and nothing looser: a server that predates the switch
      // sends no `task_notify` at all, and that reads as off — which is both
      // the pref's own default and the behaviour this branch shipped.
      set(p.task_notify?.terminal_sessions === true);
    })
    .catch(() => {
      // STILL NO ANSWER — so `false`, the shipping default. `reading` is
      // cleared so a later mount (or a publish) can ask again.
      if (generation === departed) set(false);
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer (the prefs payload a PUT returned), so the
 *  Preferences toggle takes effect without a reload. */
export function publishTaskNotifyTerminalSessions(next: boolean) {
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Current answer without subscribing; `null` until the first read lands. */
export function taskNotifyTerminalSessionsNow(): boolean | null {
  return enabled;
}

/** Test-only: forget the cached answer so a suite starts from "not asked".
 *  Notifies, like every other write. */
export function resetTaskNotifyTerminalSessionsForTests() {
  reading = null;
  generation += 1;
  set(null);
}

/** Subscribe, tri-state: `null` until the one prefs read lands. */
export function useTaskNotifyTerminalSessionsFlag(): boolean | null {
  const [current, setCurrent] = useState<boolean | null>(taskNotifyTerminalSessionsNow);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(taskNotifyTerminalSessionsNow());
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** The same subscription, flattened: should a terminal session's finished-task
 *  notice fire RIGHT NOW. What every consumer takes — "not asked yet" is
 *  honestly "no" here, matching the pref's own off-by-default shipping
 *  behaviour. */
export function useTaskNotifyTerminalSessions(): boolean {
  return useTaskNotifyTerminalSessionsFlag() === true;
}
