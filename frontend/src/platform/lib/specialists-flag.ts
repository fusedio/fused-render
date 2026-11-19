// The `specialists` experimental flag (`specialists_enabled`, shell/prefs.py;
// docs/specialists-v2.md), in platform so the Bots page, the shell and the
// chat's breakout "With" read one answer.
//
// The same module-level cache + subscribe shape as canvases-flag.ts: one read
// per page load through the single-flight `getPrefsShared` (so it shares the
// GET the chat's own feature-flag read fires — apps/claude/feature-flag.ts also
// publishes here from that read), a generation guard so a read that departed
// before a publish cannot overwrite it, and `publishSpecialistsEnabled` for the
// Preferences toggle.
//
// DEFAULT OFF: "not read yet" and "off" are the same answer, so nothing new is
// ever drawn before the server has said the flag is on.
import { useEffect, useState } from "react";
import { getPrefsShared } from "@platform/lib/api";

let enabled = false;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean) => void>();

function set(next: boolean) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of [...listeners]) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefsShared()
    .then((p) => {
      if (generation === departed) set(p?.specialists?.enabled === true);
    })
    .catch(() => {
      // A failed read is not an answer: stay where we are and let the next mount retry.
      if (generation === departed) reading = null;
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer (the toggle's PUT reply, or the chat's own
 *  prefs read in apps/claude/feature-flag.ts). */
export function publishSpecialistsEnabled(on: boolean): void {
  generation += 1;
  reading = Promise.resolve();
  set(on === true);
}

/** The publish generation, taken by another prefs reader when its GET departs
 *  (apps/claude/feature-flag.ts) and handed back with its answer. */
export function specialistsFlagStamp(): number {
  return generation;
}

/** Another reader's prefs answer, honoured only when nothing fresher was
 *  published while its GET was in flight. */
export function feedSpecialistsEnabled(on: boolean, stamp: number): void {
  if (stamp !== generation) return;
  publishSpecialistsEnabled(on);
}

/** The flag right now — a plain read for code outside React. */
export function specialistsEnabledNow(): boolean {
  return enabled;
}

/** Subscribe. Off until the first read lands. */
export function useSpecialistsEnabled(): boolean {
  const [current, setCurrent] = useState<boolean>(() => enabled);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(enabled);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** Test seam: back to "not read yet, off". Notifies mounted readers. */
export function resetSpecialistsFlagForTests(): void {
  generation += 1;
  reading = null;
  set(false);
}
