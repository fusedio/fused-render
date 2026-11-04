// Whether chats can be FAVOURITED (owner, 2026-11-03: "make sure this is
// under flag") — the `task_favourites_enabled` preference (shell/prefs.py),
// default OFF. Off, there is no star on the rail's card, no favourites
// section on the rail, no favourite's card (FavouritePeek) and no Favourites
// block in the pane; the server keeps the stars either way. The shape of
// shell/claude-rail/rail-peek-flag.ts, which it is a child of in Settings;
// here in platform because the pane (apps/claude) reads it too.
//
// ON ONLY UNDER ITS PARENT. The star lives on the rail's preview card
// (`canvas_rail_peek_enabled`), so with the card off a listed favourite could
// never be unstarred: `useTaskFavourites` answers favourites AND preview,
// both read here from the same prefs payload (platform cannot import the
// rail's own flag module), the preview half kept current by Settings'
// `publishFavouritesParent` beside the rail's own publish.
//
// LIVE ACROSS TABS: a prefs PUT made in another tab or window arrives on the
// prefs channel (api.ts `onPrefsWrittenElsewhere`) and lands like a publish;
// a window coming back re-reads (prefs-reread) for whatever the channel
// missed.
import { useEffect, useState } from "react";
import { dropSharedPrefsRead, getPrefsShared, onPrefsWrittenElsewhere, type Prefs } from "./api";
import { onPrefsReread } from "./prefs-reread";

type Half = "favs" | "peek";
// `null` = not asked yet, which reads as off (both flags' default).
const known: Record<Half, boolean | null> = { favs: null, peek: null };
// One generation PER HALF: a publish of one switch must not void an in-flight
// read's answer for the other.
const generation: Record<Half, number> = { favs: 0, peek: 0 };
let reading: Promise<void> | null = null;
const listeners = new Set<(v: boolean) => void>();

/** Read the server's way: only `true` is on. */
function on(p: { enabled?: boolean } | undefined): boolean {
  return p?.enabled === true;
}

function answer(): boolean {
  return known.favs === true && known.peek === true;
}

function set(half: Half, next: boolean) {
  if (known[half] === next) return;
  const before = answer();
  known[half] = next;
  const after = answer();
  if (after !== before) for (const listener of listeners) listener(after);
}

/** Apply a full prefs read begun when `departed` was current. */
function land(prefs: Prefs, departed: Record<Half, number>) {
  if (generation.favs === departed.favs) set("favs", on(prefs.task_favourites));
  if (generation.peek === departed.peek) set("peek", on(prefs.canvas_rail_peek));
}

/** Another window flipped a switch and this one came back: the shared re-read
 *  (prefs-reread) brings its answer here, like contrib-flags. */
function onReread(p: Promise<Prefs>): void {
  const departed = { ...generation };
  p.then((prefs) => land(prefs, departed)).catch(() => {});
}

/** Another tab's PUT: its payload is as fresh as our own publish. A payload
 *  without a field is not an answer about it (absent ≠ off here). */
function onElsewhere(p: Prefs): void {
  if (p.task_favourites !== undefined) publishTaskFavourites(p.task_favourites);
  if (p.canvas_rail_peek !== undefined) publishFavouritesParent(p.canvas_rail_peek);
}

let following = false;
function read(): Promise<void> {
  if (!following) {
    following = true;
    onPrefsReread(onReread);
    onPrefsWrittenElsewhere(onElsewhere);
  }
  if (reading) return reading;
  const departed = { ...generation };
  reading = getPrefsShared()
    .then((p) => land(p, departed))
    .catch(() => {
      // Not an answer: stay off (the default) and let the next mount ask again.
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** A known-fresh answer — the prefs payload a Settings control's PUT
 *  returned — so a page with a rail in this tab follows the switch at once. */
export function publishTaskFavourites(next: Prefs["task_favourites"]) {
  generation.favs += 1;
  set("favs", on(next));
}

/** The same for the PARENT switch (the rail's preview card), published by
 *  Settings beside `publishCanvasRailPeek`. */
export function publishFavouritesParent(next: Prefs["canvas_rail_peek"]) {
  generation.peek += 1;
  set("peek", on(next));
}

/** Subscribe: true only while favourites AND the preview card are both on.
 *  The first reader triggers the one read; later ones reuse it. */
export function useTaskFavourites(): boolean {
  const [current, setCurrent] = useState<boolean>(answer);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(answer());
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** Test-only: forget the cached answers so a suite starts from "not asked".
 *  Drops the SHARED prefs read too (api.ts): in one bun process another
 *  flag module's in-flight GET would otherwise answer for this one. */
export function resetTaskFavouritesFlagForTests() {
  dropSharedPrefsRead();
  reading = null;
  // The next read subscribes again (a test may have reset the prefs channel,
  // api.ts `resetPrefsChannelForTests`; the re-read handlers are a set).
  following = false;
  generation.favs += 1;
  generation.peek += 1;
  known.favs = null;
  known.peek = null;
  for (const listener of listeners) listener(false);
}
