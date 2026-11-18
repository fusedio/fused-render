// FREE PINS ON THE CLAUDE RAIL (owner, 2026-11-18; rail-slots-lib.ts header) —
// the `rail_free_pins_enabled` preference (shell/prefs.py), default OFF. It
// changes what the pin of rail-pin-lib means, so it is only ever read with
// the unified head on (`task_head_unified_enabled`): the pin icon exists only
// there. Both readers below answer "the pin is on AND it is a free pin".
//
// The shape of `../task-head-flag.ts`: a prefs GET of its own, a generation
// guard so a publish beats a slower in-flight read, and `null` meaning "not
// asked yet", which renders as off — the flag's own default.
import { useEffect, useState } from "react";
import { getPrefs, onPrefsWrittenElsewhere, type Prefs } from "@platform/lib/api";
import { subscribeTaskHeadUnified, taskHeadUnifiedNow, useTaskHeadUnified } from "@shell/task-head-flag";

/** The last answer, remembered across a reload (task-head-flag's reason: a
 *  pinned window must not stand docked for the first frames, then jump). */
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
 *  answer — this switch's or the unified head's, which it stands behind — and
 *  never counted as a mounted reader (a failed read is retried only while a
 *  hook is up). */
const watchers = new Set<() => void>();
function tell(): void {
  for (const fn of [...watchers]) fn();
}
subscribeTaskHeadUnified(tell);

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
  // Its own GET, not the shared one (task-head-flag `read`: a shared read
  // pinned by one suite's unanswering fetch stub held up every later suite).
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

/** Free pins are on: this switch AND the unified head's (header). */
export function useRailFreePins(): boolean {
  const unified = useTaskHeadUnified();
  const [current, setCurrent] = useState<boolean>(answer ?? false);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(answer ?? false);
    // Asked only with the unified head on: off, the answer is never read, and
    // a document that never turns it on pays no prefs GET for this switch.
    if (unified) void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, [unified]);
  return unified && current;
}

/** The current answer, for non-hook readers (stores, a press's handler). */
export function railFreePinsNow(): boolean {
  return (answer ?? false) && taskHeadUnifiedNow();
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
