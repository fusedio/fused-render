// Whether the Tasks page opens a task in a SIDE PANEL beside the list instead
// of navigating to the Explorer — the `task_peek_enabled` pref
// (fused_render/shell/prefs.py), experimental and default OFF.
//
// A CLONE OF `apps/claude/feature-flag.ts`, deliberately down to the shape: one
// shared GET, a generation guard so a publish beats a slower in-flight read, and
// `null` meaning "not asked yet". Two flags that gate a whole behaviour should
// not have two different idioms for the same three states.
//
// WHY THE TRI-STATE MATTERS HERE, which is the one thing worth saying twice:
// OFF must be behaviour-identical to the page before any of this existed, and
// that includes the frames where the answer has not landed. A premature `false`
// would be harmless (it is the shipping behaviour); a premature `true` would
// stamp `data-peek-key` on every row, claim the window's param boundary and
// adopt a `?peek=` from the URL — none of which an opted-out reader asked for.
// So every consumer takes `=== true`, and "not asked yet" is honestly "no".
//
// The peek's own STORE is separate (`task-peek-store.ts`): this answers whether
// the feature exists at all, that one answers what it is doing.
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
      // `=== true` and nothing looser: a server that predates the switch sends
      // no `task_peek` at all, and that reads as off — which is both the pref's
      // own default and the behaviour the page has always had.
      set(p.task_peek?.enabled === true);
    })
    .catch(() => {
      // STILL NO ANSWER — so `false`. Unlike the chat's flag, nothing here is
      // holding a skeleton open waiting: `false` IS the shipping behaviour, so
      // a server we cannot ask simply gets the page it has always had.
      // `reading` is cleared so a later mount (or a publish) can ask again.
      if (generation === departed) set(false);
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer (the prefs payload a PUT returned), so the
 *  Preferences toggle takes effect without a reload. */
export function publishTaskPeekEnabled(next: boolean) {
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Current answer without subscribing; `null` until the first read lands. */
export function taskPeekEnabledNow(): boolean | null {
  return enabled;
}

/** Test-only: forget the cached answer so a suite starts from "not asked".
 *  Notifies, like every other write. */
export function resetTaskPeekFlagForTests() {
  reading = null;
  generation += 1;
  set(null);
}

/** Subscribe, tri-state: `null` until the one prefs read lands. */
export function useTaskPeekFlag(): boolean | null {
  const [current, setCurrent] = useState<boolean | null>(taskPeekEnabledNow);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(taskPeekEnabledNow());
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** The same subscription, flattened: is the side peek on RIGHT NOW. What every
 *  consumer takes — "not asked yet" is honestly "no" here (see the header). */
export function useTaskPeekEnabled(): boolean {
  return useTaskPeekFlag() === true;
}
