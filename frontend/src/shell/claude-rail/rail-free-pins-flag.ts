// FREE PINS ON THE CLAUDE RAIL (owner, 2026-11-18; rail-slots-lib.ts header) —
// the `rail_free_pins_enabled` preference (shell/prefs.py), default OFF. It
// changes what the pin of rail-pin-lib means.
//
// A prefs GET of its own, a generation guard so a publish beats a slower
// in-flight read, and `null` meaning "not asked yet", which renders as off —
// the flag's own default.
import { useEffect, useState } from "react";
import { getPrefs, onPrefsWrittenElsewhere, type Prefs } from "@platform/lib/api";
import { onPrefsReread } from "@platform/lib/prefs-reread";

/** The last answer, remembered across a reload: a pinned window must not
 *  stand docked for the first frames, then jump. */
const MEMO_KEY = "fused-render:rail-free-pins";
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
const RETRY_MS = 15_000;
let retry: ReturnType<typeof setTimeout> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean) => void>();
/** Readers outside React (rail-move-store): heard on every change of the
 *  answer, and never counted as a mounted reader (a failed read is retried
 *  only while a hook is up). */
const watchers = new Set<() => void>();
function tell(): void {
  for (const fn of [...watchers]) fn();
}
function fromPrefs(p: Prefs["rail_free_pins"]): boolean {
  return p?.enabled === true;
}

function set(next: boolean) {
  memoWrite(next);
  if (answer === next) return;
  answer = next;
  for (const listener of [...listeners]) listener(next);
  tell();
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  // Its own GET, not the shared one: a shared read pinned by one suite's
  // unanswering fetch stub held up every later suite.
  reading = getPrefs()
    .then((p) => {
      if (generation === departed) set(fromPrefs(p.rail_free_pins));
    })
    .catch(() => {
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

/** A known-fresh answer (the prefs payload a Settings control's PUT returned). */
export function publishRailFreePins(next: Prefs["rail_free_pins"]) {
  generation += 1;
  reading = Promise.resolve();
  set(fromPrefs(next));
}

onPrefsWrittenElsewhere((p) => {
  if (p.rail_free_pins !== undefined) publishRailFreePins(p.rail_free_pins);
});

// ANOTHER WINDOW FLIPPED THE SWITCH AND THIS ONE CAME BACK (platform/lib
// prefs-reread; CodeRabbit on #2349): with no BroadcastChannel the write above
// never arrives, and a window would keep the answer it loaded with — free pins
// on in one window, off in the next. The shared re-read on focus brings the
// answer here, under the same generation guard; a failed read changes nothing.
// Followed from the first hook on, not at import: a
// store that only imports this module must not make a document re-read prefs.
let following = false;
function onReread(p: Promise<Prefs>): void {
  const departed = generation;
  p.then((prefs) => {
    if (generation === departed) set(fromPrefs(prefs.rail_free_pins));
  }).catch(() => {});
}

/** Free pins are on. */
export function useRailFreePins(): boolean {
  const [current, setCurrent] = useState<boolean>(answer ?? false);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(answer ?? false);
    if (!following) {
      following = true;
      onPrefsReread(onReread);
    }
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** The current answer, for non-hook readers (stores, a press's handler). */
export function railFreePinsNow(): boolean {
  return answer ?? false;
}

/** Hear the switch change, outside React (rail-move-store re-reads its
 *  positions: a pushed pin is only pushed with the switch on). */
export function subscribeRailFreePins(fn: () => void): () => void {
  watchers.add(fn);
  return () => {
    watchers.delete(fn);
  };
}

/** Test seams: forget the answer; or say it outright (no fetch). */
export function resetRailFreePinsFlagForTests() {
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
  for (const listener of [...listeners]) listener(false);
  tell();
}
export function setRailFreePinsForTests(on: boolean) {
  generation += 1;
  reading = Promise.resolve();
  set(on);
}
