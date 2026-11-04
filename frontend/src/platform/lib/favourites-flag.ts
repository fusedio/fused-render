// Whether chats can be FAVOURITED (owner, 2026-11-03: "make sure this is
// under flag") — the `task_favourites_enabled` preference (shell/prefs.py),
// default OFF. Off, there is no star on the rail's card, no favourites
// section on the rail, no favourite's card (FavouritePeek) and no Favourites
// block in the pane; the server keeps the stars either way. The shape of
// shell/claude-rail/rail-peek-flag.ts, which it is a child of in Settings;
// here in platform because the pane (apps/claude) reads it too.
import { useEffect, useState } from "react";
import { dropSharedPrefsRead, getPrefsShared, type Prefs } from "./api";
import { onPrefsReread } from "./prefs-reread";

let answer: boolean | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean) => void>();

/** The prefs payload's sub-object, read the server's way: only `true` is on. */
function fromPrefs(p: Prefs["task_favourites"]): boolean {
  return p?.enabled === true;
}

function set(next: boolean) {
  if (answer === next) return;
  answer = next;
  for (const listener of listeners) listener(next);
}

/** Another window flipped the switch: the shared re-read (prefs-reread)
 *  brings its answer here, like contrib-flags. */
function onReread(p: Promise<Prefs>): void {
  const departed = generation;
  p.then((prefs) => {
    if (generation === departed) set(fromPrefs(prefs.task_favourites));
  }).catch(() => {});
}

function read(): Promise<void> {
  onPrefsReread(onReread);
  if (reading) return reading;
  const departed = generation;
  reading = getPrefsShared()
    .then((p) => {
      if (generation === departed) set(fromPrefs(p.task_favourites));
    })
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
  generation += 1;
  reading = Promise.resolve();
  set(fromPrefs(next));
}

/** Subscribe. The first reader triggers the one read; later ones reuse it. */
export function useTaskFavourites(): boolean {
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
export function resetTaskFavouritesFlagForTests() {
  dropSharedPrefsRead();
  reading = null;
  generation += 1;
  answer = null;
  for (const listener of listeners) listener(false);
}
