// Whether a collapsed Claude rail (any page with one) shows a chat as a live
// floating card (RailPeek.tsx) — the `canvas_rail_peek_enabled` preference
// (shell/prefs.py), default OFF. A hover previews, a click pins: there is no
// trigger choice (the `canvas_rail_peek_trigger` pref of 2026-10-31 is gone;
// owner, 2026-11-02: "get rid of the flag dropdown … click only").
//
// The shape of `./feature-flag.ts` (the Canvases switch itself): one shared
// prefs GET, a generation guard so a publish beats a slower in-flight read,
// and `null` meaning "not asked yet", which renders as off — the flag's own
// default, so a rail never grows a card the reader turned off because a GET
// was still out.
import { useEffect, useState } from "react";
import { useTaskFavourites } from "@platform/lib/favourites-flag";
import { dropSharedPrefsRead, getPrefsShared, onPrefsWrittenElsewhere, type Prefs } from "@platform/lib/api";

/** THE LAST ANSWER, REMEMBERED ACROSS A RELOAD (owner, 2026-11-09: "when we
 *  reload … the rail shows up a moment after, and the Tasks and Bots icons
 *  jump from the left sidebar to the right"): until the prefs GET answered
 *  this read as off, so the left sidebar drew its Tasks/Bots rows and the
 *  rail had no nav doors for the first frames. The last answer is kept in
 *  localStorage (every window, every launch) and is the starting point; the
 *  GET corrects it. Nothing but a boolean is kept. */
const MEMO_KEY = "fused-render:rail-peek-on";
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
/** A failed read is retried while a reader is mounted (CodeRabbit on #2205):
 *  the remembered answer stands until the GET answers, never for good. */
const RETRY_MS = 15_000;
let retry: ReturnType<typeof setTimeout> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean) => void>();

/** The prefs payload's sub-object, read the server's way: only `true` is on. */
function fromPrefs(p: Prefs["canvas_rail_peek"]): boolean {
  return p?.enabled === true;
}

function set(next: boolean) {
  memoWrite(next);
  if (answer === next) return;
  answer = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefsShared()
    .then((p) => {
      if (generation === departed) set(fromPrefs(p.canvas_rail_peek));
    })
    .catch(() => {
      // Not an answer: the remembered one (or off) stands; ask again while a
      // reader is up, and the next mount asks too.
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

/** A known-fresh answer — the prefs payload a Settings control's PUT
 *  returned — so a page with a rail in this tab follows the switch at once. */
export function publishCanvasRailPeek(next: Prefs["canvas_rail_peek"]) {
  generation += 1;
  reading = Promise.resolve();
  set(fromPrefs(next));
}

// ANOTHER TAB'S PUT (2026-11-04): the prefs channel carries every written
// payload; one that names this switch lands like a publish, so the rail here
// follows a Settings page open elsewhere without a reload.
onPrefsWrittenElsewhere((p) => {
  if (p.canvas_rail_peek !== undefined) publishCanvasRailPeek(p.canvas_rail_peek);
});

/** Subscribe. The first reader triggers the one read; later ones reuse it. */
export function useCanvasRailPeek(): boolean {
  const [current, setCurrent] = useState<boolean>(answer ?? false);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(answer ?? false);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** Test-only: forget the cached answer so a suite starts from "not asked".
 *  Drops the SHARED prefs read too (api.ts): in one bun process another
 *  flag module's in-flight GET would otherwise answer for this one. */
export function resetCanvasRailPeekFlagForTests() {
  dropSharedPrefsRead();
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
  for (const listener of listeners) listener(false);
}

/** THE ONE GATE for everything the favourites rail adds (ClaudeRail's
 *  `favsOn`, FallbackRail): the favourites switch AND the preview card's. */
export function useRailFeatureOn(): boolean {
  const favourites = useTaskFavourites();
  const peekOn = useCanvasRailPeek();
  return favourites && peekOn;
}
