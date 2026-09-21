// Whether the APP PAGE's Tasks tab (`/apps/<folder>?_tab=tasks`) opens a task
// in the same side panel `/tasks` does, instead of navigating to the Explorer
// as that tab always has — the `project_peek_enabled` pref
// (fused_render/shell/prefs.py), default OFF: a feature flag on that one
// surface while it settles (Akshil, 2026-09-21). `/tasks`'s own peek is not
// gated by this (task-peek-flag.ts).
//
// A CLONE OF `task-notify-terminal-flag.ts`, deliberately down to the shape:
// one shared GET, a generation guard so a publish beats a slower in-flight
// read, and `null` meaning "not asked yet". The tri-state matters here for
// the same reason it does on the Tasks page: a premature `true` would stamp
// walk attributes on every row and adopt a `?peek=` from the URL on behalf
// of a reader who has the flag OFF — so the first frames stay `null`, every
// consumer takes `=== true`, and the tab navigates as it always did until the
// read lands a few milliseconds later.
//
// THE GATES live in AppPage.tsx (`peekable`) and Scheduled.tsx (scoped) —
// this module only answers "is the preference on".
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
      // sends no `project` under `task_peek`, and that reads as off — the
      // pref's own default, and the tab's behaviour before the flag existed.
      set(p.task_peek?.project === true);
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
export function publishProjectPeekEnabled(next: boolean) {
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Current answer without subscribing; `null` until the first read lands. */
export function projectPeekEnabledNow(): boolean | null {
  return enabled;
}

/** Test-only: forget the cached answer so a suite starts from "not asked".
 *  Notifies, like every other write. */
export function resetProjectPeekFlagForTests() {
  reading = null;
  generation += 1;
  set(null);
}

/** Subscribe, tri-state: `null` until the one prefs read lands. */
export function useProjectPeekFlag(): boolean | null {
  const [current, setCurrent] = useState<boolean | null>(projectPeekEnabledNow);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(projectPeekEnabledNow());
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** The same subscription, flattened: does the app page's Tasks tab host the
 *  peek RIGHT NOW. "Not asked yet" is honestly "no" here — the tab then
 *  navigates exactly as it did before the flag, which is also the default. */
export function useProjectPeekEnabled(): boolean {
  return useProjectPeekFlag() === true;
}
