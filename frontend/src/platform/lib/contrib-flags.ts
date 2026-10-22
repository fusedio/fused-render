// The five phase switches of the GitHub contribution loop (docs/CONTRIB_LOOP.md
// §1) — `contrib_<phase>_enabled` in shell/prefs.py, all opt-in, default off.
// The server answers the RAW values under `prefs.contrib` (Preferences shows
// the switches as set); everything else reads the EFFECTIVE values this module
// derives, with the prerequisites applied exactly as fused_render/contrib/
// flags.py applies them server-side:
//
//   sources  itself                      (A1 sources + share by link)
//   changes  sources && itself           (A2 your changes + Propose)
//   inbox    sources && itself           (A3 author inbox)
//   loop     changes && inbox && itself  (A4 merge closes the loop)
//   remix    changes && itself           (A5 remix)
//
// Same shape as github-link-flag.ts — ONE FETCH, NOT A POLL: the prefs can
// only change on this app's Preferences page, and that page PUBLISHES the new
// values (and another window's flip is picked up when this one comes back —
// prefs-reread.ts). The `generation` counter keeps a pre-toggle GET landing after the
// publish from writing the old values back. Plus a synchronous getter like
// share-app-flag.ts `appSharingEnabled()`, for callers that are plain
// functions built at click time (appCardMenu) rather than components.
import { useEffect, useState } from "react";
import { getPrefsShared, type ContribPrefs, type Prefs } from "@platform/lib/api";
import { onPrefsReread } from "@platform/lib/prefs-reread";

/** Effective phase values — prerequisites applied. */
export interface ContribFlags {
  sources: boolean;
  changes: boolean;
  inbox: boolean;
  loop: boolean;
  remix: boolean;
}

const OFF: ContribFlags = { sources: false, changes: false, inbox: false, loop: false, remix: false };

/** Raw switch → per-phase flags (every phase is the one switch). */
export function effectiveContribFlags(raw: Partial<ContribPrefs> | null | undefined): ContribFlags {
  // `=== true`: opt-in, so a server that predates the key reads it as off.
  const on = raw?.enabled === true;
  return { sources: on, changes: on, inbox: on, loop: on, remix: on };
}

/** The last answer, or `null` while nobody has asked yet. Null reads as all
 *  OFF (the defaults), so nothing flashes in before the answer lands. */
let flags: ContribFlags | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: ContribFlags) => void>();

function same(a: ContribFlags | null, b: ContribFlags): boolean {
  return (
    !!a &&
    a.sources === b.sources &&
    a.changes === b.changes &&
    a.inbox === b.inbox &&
    a.loop === b.loop &&
    a.remix === b.remix
  );
}

function set(next: ContribFlags) {
  // `same` is false for a null `flags`, so a first all-off answer still
  // publishes — listeners need null → OFF to stop waiting.
  if (same(flags, next)) return;
  flags = next;
  for (const listener of listeners) listener(next);
}

// Another window may have flipped a switch: re-read when this one comes back
// (prefs-reread.ts — one shared GET for every flag module). Same guard as the
// first read; a failed re-read keeps what is shown.
function onReread(p: Promise<Prefs>) {
  const departed = generation;
  p.then((prefs) => {
    if (generation === departed) set(effectiveContribFlags(prefs.contrib));
  }).catch(() => {});
}

function read(): Promise<void> {
  onPrefsReread(onReread);
  if (reading) return reading;
  const departed = generation;
  reading = getPrefsShared() // single-flight across flag modules (api.ts)
    .then((p) => {
      if (generation === departed) set(effectiveContribFlags(p.contrib));
    })
    .catch(() => {
      // A failed read settles as ALL OFF when nothing better is known — the
      // defaults, so every surface behaves exactly as with the loop off
      // rather than waiting on `null` (the share sheet's Fused link card
      // holds its reads until this answers). `reading` is cleared so the
      // next mount still asks again and can upgrade it.
      reading = null;
      if (flags === null && generation === departed) set(OFF);
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer — the RAW `contrib` block of the prefs
 *  payload a PUT returned. Called by the Preferences page's switches. */
export function publishContribFlags(raw: Partial<ContribPrefs> | null | undefined) {
  // Bump FIRST so a read already in flight drops its (stale) answer.
  generation += 1;
  reading = Promise.resolve();
  set(effectiveContribFlags(raw));
}

/** The last answer, synchronously — for callers that are not components.
 *  All off until a read has landed. */
export function contribFlagsSync(): ContribFlags {
  void read();
  return flags ?? OFF;
}

/** Subscribe. `null` until the first read lands; the first reader triggers
 *  the one read, later ones reuse it. */
export function useContribFlags(): ContribFlags | null {
  const [current, setCurrent] = useState<ContribFlags | null>(flags);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(flags);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}
