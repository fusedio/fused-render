// The `experts` experimental flag (`experts_enabled`, shell/prefs.py;
// docs/experts.md), in platform so the Bots page, the shell and the
// chat's breakout "With" read one answer.
//
// The same module-level cache + subscribe shape as canvases-flag.ts: one read
// per page load through the single-flight `getPrefsShared` (shared with the
// other flag modules), a generation guard so a read that departed
// before a publish cannot overwrite it, and `publishExpertsEnabled` for the
// Preferences toggle.
//
// DEFAULT OFF: "not read yet" and "off" are the same answer, so nothing new is
// ever drawn before the server has said the flag is on.
import { useEffect, useState } from "react";
import { getPrefsShared, onPrefsWrittenElsewhere } from "@platform/lib/api";

let enabled = false;
/** An answer has landed (a read or a publish): until then `enabled` false means "not known yet", not "off". */
let settled = false;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean) => void>();

const knownListeners = new Set<(v: boolean | null) => void>();

function set(next: boolean) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of [...listeners]) listener(next);
}

/** `set` for an answer (a read or a publish): also tells the readers that need "known off" apart from "not read". */
function answer(next: boolean) {
  set(next);
  settled = true;
  for (const listener of [...knownListeners]) listener(enabled);
}

let following = false;
/** The first answer (or the one published since): what `expertsEnabledNow` reads once it settles. */
export function readExpertsFlag(): Promise<void> {
  if (!following) {
    // The toggle flipped in another window lands like a publish here (as favourites-flag.ts does).
    following = true;
    onPrefsWrittenElsewhere((p) => { if (p.experts !== undefined) publishExpertsEnabled(p.experts.enabled === true); });
  }
  if (reading) return reading;
  const departed = generation;
  reading = getPrefsShared()
    .then((p) => {
      if (generation === departed) answer(p?.experts?.enabled === true);
    })
    .catch(() => {
      // A failed read is not an answer: stay where we are and let the next mount retry.
      if (generation === departed) reading = null;
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer (the toggle's PUT reply). */
export function publishExpertsEnabled(on: boolean): void {
  generation += 1;
  reading = Promise.resolve();
  answer(on === true);
}

/** The flag right now — a plain read for code outside React. */
export function expertsEnabledNow(): boolean {
  return enabled;
}

/** Subscribe. Off until the first read lands. */
export function useExpertsEnabled(): boolean {
  const [current, setCurrent] = useState<boolean>(() => enabled);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(enabled);
    void readExpertsFlag();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** Subscribe, telling "off" from "not read yet": `null` until the first answer lands. For a reader that would
 *  otherwise act on an off it has not heard yet (the shell's redirect away from a `/bots` URL, Windows). */
export function useExpertsFlagKnown(): boolean | null {
  const [current, setCurrent] = useState<boolean | null>(() => (settled ? enabled : null));
  useEffect(() => {
    knownListeners.add(setCurrent);
    setCurrent(settled ? enabled : null);
    void readExpertsFlag();
    return () => {
      knownListeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** Test seam: back to "not read yet, off". Notifies mounted readers. */
export function resetExpertsFlagForTests(): void {
  generation += 1;
  reading = null;
  set(false);
  settled = false;
  for (const listener of [...knownListeners]) listener(null);
}
