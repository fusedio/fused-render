// Whether every task wears a GLYPH in place of its status ring — the
// `task_icons_enabled` preference (shell/prefs.py), default OFF, an
// experiment (owner, 2026-11-10: "do this under experiment flag"). One of 299
// line drawings (shell/task-icons.ts), chosen server-side from the task's
// number so no two tasks in a project share one (`Task.icon_slot`); off, the
// ring is drawn as it always was and the slot the listing sends is ignored.
//
// The single-flag half of favourites-flag.ts: a localStorage memo so a reload
// draws the right mark on its first frame, one generation so a publish beats
// a slower in-flight read, and the prefs channel for a switch flipped in
// another tab. In platform because the ring (shell/ScheduleTaskViews
// StatusIcon) is drawn from the pane (apps/claude) too.
import { useEffect, useState } from "react";
import { dropSharedPrefsRead, getPrefsShared, onPrefsWrittenElsewhere, type Prefs } from "./api";
import { onPrefsReread } from "./prefs-reread";

const MEMO_KEY = "fused-render:task-icons-on";
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

// `null` = not asked yet, which reads as off (the default).
let known: boolean | null = memoRead();
let generation = 0;
let reading: Promise<void> | null = null;
const listeners = new Set<(v: boolean) => void>();

/** Read the server's way: only `true` is on. */
function on(p: { enabled?: boolean } | undefined): boolean {
  return p?.enabled === true;
}

function answer(): boolean {
  return known === true;
}

function set(next: boolean) {
  if (known === next) return;
  const before = answer();
  known = next;
  memoWrite(next);
  if (answer() !== before) for (const listener of listeners) listener(next);
}

function land(prefs: Prefs, departed: number) {
  if (generation === departed) set(on(prefs.task_icons));
}

function onReread(p: Promise<Prefs>): void {
  const departed = generation;
  p.then((prefs) => land(prefs, departed)).catch(() => {});
}

/** Another tab's PUT. A payload without the field is not an answer about it. */
function onElsewhere(p: Prefs): void {
  if (p.task_icons !== undefined) publishTaskIcons(p.task_icons);
}

let following = false;
function read(): Promise<void> {
  if (!following) {
    following = true;
    onPrefsReread(onReread);
    onPrefsWrittenElsewhere(onElsewhere);
  }
  if (reading) return reading;
  const departed = generation;
  reading = getPrefsShared()
    .then((p) => land(p, departed))
    .catch(() => {
      // Not an answer: stay off (the default) and let the next mount ask again.
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** A known-fresh answer — the prefs payload a Settings control's PUT
 *  returned — so every ring in this tab follows the switch at once. */
export function publishTaskIcons(next: Prefs["task_icons"]) {
  generation += 1;
  set(on(next));
}

/** Subscribe: true while the experiment is on. The first reader triggers the
 *  one read; later ones reuse it. */
export function useTaskIcons(): boolean {
  const [current, setCurrent] = useState<boolean>(answer);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(answer());
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** Test-only: forget the cached answer so a suite starts from "not asked". */
export function resetTaskIconsFlagForTests() {
  try {
    localStorage.removeItem(MEMO_KEY);
  } catch {
    /* no storage */
  }
  dropSharedPrefsRead();
  reading = null;
  following = false;
  generation += 1;
  known = null;
  for (const listener of listeners) listener(false);
}
