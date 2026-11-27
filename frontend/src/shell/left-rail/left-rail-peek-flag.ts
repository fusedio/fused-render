// SIDEBAR PREVIEWS (owner, 2026-11-27) — the `left_rail_peek_enabled`
// preference (shell/prefs.py), default OFF. On, the collapsed left sidebar's
// icons open the Claude rail's window A card on hover (left-peek-store,
// LeftPeek). Off, the rail is exactly what it was: no card, no hooks on the
// icons.
//
// The shape of rail-extras-flag.ts (a prefs GET of its own, a generation
// guard so a publish beats a slower in-flight read, `null` meaning "not asked
// yet", read as off) WITHOUT its remembered answer: nothing here is drawn
// before a hover, so a first hover a moment after the load costs nothing, and
// the switch keeps no browser storage of its own.
import { useEffect, useState } from "react";
import { getPrefs, onPrefsWrittenElsewhere, type Prefs } from "@platform/lib/api";
import { onPrefsReread } from "@platform/lib/prefs-reread";

let answer: boolean | null = null;
let reading: Promise<void> | null = null;
/** A failed read is retried while a reader is mounted. */
const RETRY_MS = 15_000;
let retry: ReturnType<typeof setTimeout> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean | null) => void>();
/** Readers outside React (left-peek-store): heard on every change. */
const watchers = new Set<() => void>();

/** The prefs payload's sub-object, read the server's way: only `true` is on. */
function fromPrefs(p: Prefs["left_rail_peek"]): boolean {
  return p?.enabled === true;
}

function set(next: boolean) {
  if (answer === next) return;
  answer = next;
  for (const listener of [...listeners]) listener(next);
  for (const fn of [...watchers]) fn();
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefs()
    .then((p) => {
      if (generation === departed) set(fromPrefs(p.left_rail_peek));
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
export function publishLeftRailPeek(next: Prefs["left_rail_peek"]) {
  generation += 1;
  reading = Promise.resolve();
  set(fromPrefs(next));
}

// Another tab's PUT: a payload without the field is not an answer about it.
onPrefsWrittenElsewhere((p) => {
  if (p.left_rail_peek !== undefined) publishLeftRailPeek(p.left_rail_peek);
});

// Another window flipped the switch and this one came back (platform/lib
// prefs-reread), followed from the first hook on (rail-extras-flag's rule).
let following = false;
// A re-read is a NEWER read than any still out (CodeRabbit on #2495): it
// takes a generation of its own, so the first read landing after it is old
// news. Failed, it changes nothing — and if the read it outdated was the
// only answer coming, a fresh read is asked for.
function onReread(p: Promise<Prefs>): void {
  generation += 1;
  const departed = generation;
  p.then((prefs) => {
    if (generation === departed) set(fromPrefs(prefs.left_rail_peek));
  }).catch(() => {
    if (generation !== departed || answer !== null) return;
    reading = null;
    if (listeners.size > 0) void read();
  });
}

/** The switch is on (not answered yet reads as off, the flag's default). The
 *  first reader triggers the one read; later ones reuse it. */
export function useLeftRailPeek(): boolean {
  const [current, setCurrent] = useState<boolean | null>(answer);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(answer);
    if (!following) {
      following = true;
      onPrefsReread(onReread);
    }
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current === true;
}

/** The current answer, for non-hook readers. */
export function leftRailPeekNow(): boolean {
  return answer === true;
}

/** Hear the switch change, outside React. */
export function subscribeLeftRailPeek(fn: () => void): () => void {
  watchers.add(fn);
  return () => {
    watchers.delete(fn);
  };
}

/** Test seams: forget the answer; or say it outright (no fetch). */
export function resetLeftRailPeekFlagForTests() {
  reading = null;
  if (retry) clearTimeout(retry);
  retry = null;
  generation += 1;
  answer = null;
  for (const listener of [...listeners]) listener(null);
  for (const fn of [...watchers]) fn();
}
/** A re-read's answer, as platform/lib/prefs-reread hands it (tests). */
export function rereadLeftRailPeekForTests(p: Promise<Prefs>): void {
  onReread(p);
}
export function setLeftRailPeekForTests(on: boolean) {
  publishLeftRailPeek({ enabled: on });
}
