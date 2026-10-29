// The five phases of the GitHub contribution loop (docs/CONTRIB_LOOP.md §1).
// The loop itself (sources, changes, inbox, loop) is ALWAYS ON since
// 2026-10-29 — it was one opt-in switch, `contrib_enabled`; only Remix keeps
// a switch (`contrib_remix_enabled` in shell/prefs.py, opt-in, default off).
// The server answers the RAW values under `prefs.contrib` (Preferences shows
// the Remix switch as set); everything else reads the EFFECTIVE values this
// module derives, exactly as fused_render/contrib/flags.py does server-side:
//
//   sources  on                          (A1 sources + share by link)
//   changes  on                          (A2 your changes + Propose)
//   inbox    on                          (A3 author inbox)
//   loop     on                          (A4 merge closes the loop)
//   remix    itself                      (A5 remix)
//
// Same shape as share-app-flag.ts — ONE FETCH, NOT A POLL: the prefs can
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

/** The defaults: the loop on, Remix (opt-in) off. */
const OFF: ContribFlags = { sources: true, changes: true, inbox: true, loop: true, remix: false };

/** Raw switches → per-phase flags. The loop's four phases are always on;
 *  Remix is its own switch (`=== true`: opt-in, so a server that predates
 *  the key reads it as off). */
export function effectiveContribFlags(raw: Partial<ContribPrefs> | null | undefined): ContribFlags {
  return { sources: true, changes: true, inbox: true, loop: true, remix: raw?.remix === true };
}

/** The last answer, or `null` while nobody has asked yet. Null reads as the
 *  defaults (loop on, Remix off), so nothing flashes in before the answer
 *  lands. */
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
      // A failed read settles as the DEFAULTS when nothing better is known,
      // so every surface behaves as with Remix off rather than waiting on
      // `null` (the share sheet's Fused link card
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
 *  The defaults until a read has landed. */
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
