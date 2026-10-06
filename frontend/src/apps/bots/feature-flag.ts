// Whether the Bots sub-app is this machine's FRONT DOOR — the `bots_enabled`
// preference (shell/prefs.py), default off. Off: the sidebar reads Home →
// Tasks and `/` lands on /home, with no Bots row at all. On: the Home row is
// hidden, Bots sits where Home sat (Bots → Tasks) and `/` lands on /bots.
//
// A SEPARATE MODULE FROM index.ts, as @apps/canvases/feature-flag is: the
// barrel re-exports the whole bots page, and a sidebar that read one boolean
// through it would pull the bots app into the shell's main bundle.
//
// Same store shape as the canvases flag (one fetch, publish from Preferences,
// generation counter against a stale in-flight read), plus one thing that flag
// never needed: a SYNCHRONOUS seed and read. The shell decides where `/` goes
// at its first render, before any prefs GET could answer, so `/api/config`
// carries the flag and App.tsx seeds this module from it, then asks
// `botsFrontDoor()` with no await.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";

let enabled: boolean | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean) => void>();

function set(next: boolean) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefs()
    .then((p) => {
      if (generation === departed) set(p.bots.enabled);
    })
    .catch(() => {
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** Seed from the config payload (App.tsx, once per load). A seed is an
 *  answer, so no prefs GET follows it until a publish says otherwise. */
export function seedBotsEnabled(value: boolean | undefined) {
  if (typeof value !== "boolean" || enabled !== null) return;
  generation += 1;
  reading = Promise.resolve();
  enabled = value;
}

/** The current answer, synchronously — for render-time routing decisions.
 *  Null (never seeded, never read) is OFF, the default. */
export function botsFrontDoor(): boolean {
  return enabled === true;
}

/** Hand over a known-fresh answer — the prefs payload a PUT returned
 *  (Preferences page). Bump the generation FIRST so an in-flight read drops
 *  its pre-toggle value. */
export function publishBotsEnabled(next: boolean) {
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Subscribe. The first reader triggers the one read; later ones reuse it. */
export function useBotsFeature(): boolean {
  const [current, setCurrent] = useState(enabled === true);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(enabled === true);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}
