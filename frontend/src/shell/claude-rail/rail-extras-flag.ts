// RAIL EXTRAS (owner, 2026-11-23: one switch for three) — the
// `rail_extras_enabled` preference (shell/prefs.py), default OFF. On, the
// Claude rail gains:
//   - a file page's other views as doors above Git (ClaudeRail `fileModes`,
//     mode-peek-store, ModePeek);
//   - the expandable rail: a button at its top left widens it to icon +
//     title, its inner edge drags its width (ClaudeRail `expandOn`;
//     platform/lib/railstate holds the expanded state and width);
//   - FREE PINS (rail-slots-lib.ts header): a pinned rail window keeps its
//     exact place and size, and the rail's column goes to a second, non-
//     pinned window — up to four at once. It changes what the pin of
//     rail-pin-lib means.
// Off, the plain rail.
//
// A prefs GET of its own (a shared read pinned by one suite's unanswering
// fetch stub held up every later suite), a generation guard so a publish
// beats a slower in-flight read, `null` meaning "not asked yet" (rendered as
// off, the flag's own default), and the last answer remembered across a
// reload so the doors and the button do not pop in a moment after the rail
// and a pinned window does not stand docked for its first frames, then jump.
import { useEffect, useState } from "react";
import { getPrefs, onPrefsWrittenElsewhere, type Prefs } from "@platform/lib/api";
import { onPrefsReread } from "@platform/lib/prefs-reread";

const MEMO_KEY = "fused-render:rail-extras";
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
/** A failed read is retried while a reader is mounted. */
const RETRY_MS = 15_000;
let retry: ReturnType<typeof setTimeout> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean | null) => void>();
/** Readers outside React (rail-move-store): heard on every change of the
 *  answer, and never counted as a mounted reader (a failed read is retried
 *  only while a hook is up). */
const watchers = new Set<() => void>();
function tell(): void {
  for (const fn of [...watchers]) fn();
}

/** The prefs payload's sub-object, read the server's way: only `true` is on. */
function fromPrefs(p: Prefs["rail_extras"]): boolean {
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
  reading = getPrefs()
    .then((p) => {
      if (generation === departed) set(fromPrefs(p.rail_extras));
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
export function publishRailExtras(next: Prefs["rail_extras"]) {
  generation += 1;
  reading = Promise.resolve();
  set(fromPrefs(next));
}

// Another tab's PUT: the prefs channel carries every written payload. A
// payload without the field is not an answer about it.
onPrefsWrittenElsewhere((p) => {
  if (p.rail_extras !== undefined) publishRailExtras(p.rail_extras);
});

// ANOTHER WINDOW FLIPPED THE SWITCH AND THIS ONE CAME BACK (platform/lib
// prefs-reread): with no BroadcastChannel the write above never arrives, and
// a window would keep the answer it loaded with. The shared re-read on focus
// brings the answer here, under the same generation guard; a failed read
// changes nothing. Followed from the first hook on, not at import: a store
// that only imports this module must not make a document re-read prefs.
let following = false;
function onReread(p: Promise<Prefs>): void {
  const departed = generation;
  p.then((prefs) => {
    if (generation === departed) set(fromPrefs(prefs.rail_extras));
  }).catch(() => {});
}

/** The switch as it is KNOWN: on, off, or `null` — not answered yet (no
 *  remembered answer and the read still out). The first reader triggers the
 *  one read; later ones reuse it. ClaudeRail reads `null` as "do not judge a
 *  saved mode pin yet". */
export function useRailExtrasAnswer(): boolean | null {
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
  return current;
}

/** The switch is on (not answered yet reads as off, the flag's default). */
export function useRailExtras(): boolean {
  return useRailExtrasAnswer() === true;
}

/** The current answer, for non-hook readers (stores, a press's handler). */
export function railExtrasNow(): boolean {
  return answer === true;
}

/** Hear the switch change, outside React (rail-move-store re-reads its
 *  positions: a pushed pin is only pushed with free pins on). */
export function subscribeRailExtras(fn: () => void): () => void {
  watchers.add(fn);
  return () => {
    watchers.delete(fn);
  };
}

/** Test seams: forget the answer; or say it outright (no fetch). */
export function resetRailExtrasFlagForTests() {
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
  for (const listener of [...listeners]) listener(null);
  tell();
}
export function setRailExtrasForTests(on: boolean) {
  publishRailExtras({ enabled: on });
}
