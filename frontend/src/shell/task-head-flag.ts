// Whether every task view draws ONE shared task header (TaskHead.tsx) — the
// Tasks page side panel, the rail Tasks card, the New task card and the Cards
// popup — instead of each view's own. The `task_head_unified_enabled`
// preference (shell/prefs.py), default OFF. PR 1 of the unification
// (design.md): the old heads stay as they are until the switch is on.
//
// The shape of `./claude-rail/rail-peek-flag.ts`: a prefs GET of its own (see `read`), a
// generation guard so a publish beats a slower in-flight read, and `null`
// meaning "not asked yet", which renders as off — the flag's own default.
import { useEffect, useState } from "react";
import { getPrefs, onPrefsWrittenElsewhere, type Prefs } from "@platform/lib/api";

/** THE LAST ANSWER, REMEMBERED ACROSS A RELOAD: until the prefs GET answered
 *  this read as off, so a task view would draw its old header for the first
 *  frames and then swap. The last answer is kept in localStorage and is the
 *  starting point; the GET corrects it. Nothing but a boolean is kept. */
const MEMO_KEY = "fused-render:task-head-unified";
function memoRead(): boolean | null {
  try {
    const v = localStorage.getItem(MEMO_KEY);
    return v === "1" ? true : v === "0" ? false : null;
  } catch {
    return null;
  }
}
function memoWrite(v: boolean): void {
  try {
    localStorage.setItem(MEMO_KEY, v ? "1" : "0");
  } catch {
    /* no storage: nothing remembered */
  }
}

let answer: boolean | null = memoRead();
let reading: Promise<void> | null = null;
/** A failed read is retried while a reader is mounted: the remembered answer
 *  stands until the GET answers, never for good. */
const RETRY_MS = 15_000;
let retry: ReturnType<typeof setTimeout> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean) => void>();

/** The prefs payload's sub-object, read the server's way: only `true` is on. */
function fromPrefs(p: Prefs["task_head_unified"]): boolean {
  return p?.enabled === true;
}

function set(next: boolean) {
  memoWrite(next);
  if (answer === next) return;
  answer = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  // ITS OWN GET, NOT THE SHARED ONE (`getPrefsShared`, api.ts). This hook is
  // mounted by every task view — the chat panes, the Tasks panel, the rail
  // cards, the New task card — so in `bun test` (one process, every suite)
  // any file that mounts one under a fetch stub that never answers left the
  // SHARED read pinned for the rest of the run, and the chat controller's
  // `queueFlagReady` then sat out its whole 8 s deadline in a later file
  // (red on CI's file order only: run-controller "a decide writes back what
  // LANDED on disk"). A read of our own can only ever hold up this flag,
  // whose unanswered state is the default (off). One extra local GET per page
  // load, for as long as this flag exists.
  reading = getPrefs()
    .then((p) => {
      if (generation === departed) set(fromPrefs(p.task_head_unified));
    })
    .catch(() => {
      // Not an answer: the remembered one (or off) stands; ask again while a
      // reader is up, and the next mount asks too.
      reading = null;
      if (listeners.size > 0 && !retry) {
        retry = setTimeout(() => {
          retry = null;
          if (listeners.size > 0) void read();
        }, RETRY_MS);
      }
    })
    .then(() => {});
  return reading;
}

/** A known-fresh answer — the prefs payload a Settings control's PUT
 *  returned — so every task view in this tab follows the switch at once. */
export function publishTaskHeadUnified(next: Prefs["task_head_unified"]) {
  generation += 1;
  reading = Promise.resolve();
  set(fromPrefs(next));
}

// ANOTHER TAB'S PUT: the prefs channel carries every written payload; one that
// names this switch lands like a publish.
onPrefsWrittenElsewhere((p) => {
  if (p.task_head_unified !== undefined) publishTaskHeadUnified(p.task_head_unified);
});

/** Subscribe. The first reader triggers the one read; later ones reuse it. */
export function useTaskHeadUnified(): boolean {
  const [current, setCurrent] = useState<boolean>(answer ?? false);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(answer ?? false);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** The current answer, for non-hook readers (null = not asked yet = off). */
export function taskHeadUnifiedNow(): boolean {
  return answer ?? false;
}

/** Test-only: forget the cached answer so a suite starts from "not asked". */
export function resetTaskHeadUnifiedFlagForTests() {
  reading = null;
  if (retry) clearTimeout(retry);
  retry = null;
  generation += 1;
  answer = null;
  try {
    localStorage.removeItem(MEMO_KEY);
  } catch {
    /* no storage */
  }
  for (const listener of listeners) listener(false);
}
