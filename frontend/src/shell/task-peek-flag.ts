// Whether the Tasks page opens a task in a SIDE PANEL beside the list instead
// of navigating to the Explorer — the `task_peek_enabled` pref
// (fused_render/shell/prefs.py), default ON since 2026-09-17.
//
// A CLONE OF `apps/claude/feature-flag.ts`, deliberately down to the shape: one
// shared GET, a generation guard so a publish beats a slower in-flight read, and
// `null` meaning "not asked yet". Two flags that gate a whole behaviour should
// not have two different idioms for the same three states.
//
// WHY THE TRI-STATE STILL MATTERS, now that the default is ON: "not asked yet"
// is still not an answer. A premature `true` would stamp `data-peek-key` on
// every row, claim the window's param boundary and adopt a `?peek=` from the
// URL on behalf of a reader who may have switched the panel OFF — so the first
// frames stay `null`, every consumer takes `=== true`, and the page navigates
// as it always did until the read lands a few milliseconds later.
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
      // `!== false`, matching the pref's own default (shell/prefs.py
      // `task_peek_enabled`): the panel is what a server that has never been
      // told otherwise does, so an absent field — a server that predates the
      // switch — is ON, not off.
      set(p.task_peek?.enabled !== false);
    })
    .catch(() => {
      // STILL NO ANSWER — so the pref's own default, which is now `true`
      // (2026-09-17). Nothing here is holding a skeleton open waiting either
      // way; the choice is only "which behaviour does a dropped request get",
      // and the honest answer is the one the server would have given.
      // `reading` is cleared so a later mount (or a publish) can ask again.
      if (generation === departed) set(true);
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
