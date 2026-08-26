// Query-param IO for the playground's stages.
//
// These three lived in `AiModelsPlayground.tsx` — the page component the
// stages are children of — so TextStage/ImageStage/TranscribeStage each
// imported their own parent to read a URL param. A child importing its parent
// for a utility is a cycle waiting to be one, and it meant the stages could
// not be reasoned about (or tested) without pulling the whole picker, the
// catalog fetch and the runtime subscription in behind them.
//
// Nothing here knows what a model or a stage is: it is `location.search` in
// and out. That is the whole reason it is in `lib/` rather than `playground/`
// — the page shell reads `?cap=` through the same door (see routes.ts).
import { navigateUrl, replaceSearch } from "@platform/lib/router";

// How a rewrite lands in history. `replace` (the default) edits the current
// entry — a slider drag must not stack entries. `push` adds one, so the Back
// button undoes it: that is for a MODEL change, which is a move between
// places rather than a tweak to the place one is in.
export type WriteMode = "replace" | "push";
const land = (url: string, mode: WriteMode) =>
  mode === "push" ? navigateUrl(url) : replaceSearch(url);

/** Read one query param off the CURRENT url. The second argument exists so
 *  tests can drive this without a `location`, the same three-argument trick
 *  `tabHref` takes in routes.ts and for the same reason. */
export function readParam(key: string, search: string = location.search): string | null {
  return new URLSearchParams(search).get(key);
}

/** A numeric param, defensively: a shared link is exactly where a malformed or
 *  empty value arrives, and `Number("")` is 0 — a temperature nobody chose.
 *
 *  `min`/`max` CLAMP rather than reject, and passing them is not optional
 *  politeness wherever the server validates the same number. The sampling
 *  route REFUSES an out-of-range value (`_sampling_problem`, server/ai.py)
 *  instead of clamping it, so `?temp=5` used to seed the rail with 5 and then
 *  400 on every single message — a link that opens a permanently broken chat.
 *  The rail's own number input already clamps to the same bounds; this closes
 *  the door the URL left open. */
export function numParam(
  key: string,
  fallback: number,
  min?: number,
  max?: number,
  search?: string,
): number {
  const raw = readParam(key, search ?? location.search);
  if (raw === null || raw.trim() === "") return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value)) return fallback;
  if (min !== undefined && value < min) return min;
  if (max !== undefined && value > max) return max;
  return value;
}

/** The `?a=b&c=d` for a set of params — "" when they are all null. Pure, and
 *  exported for that reason: the two writers below need a `location` and a
 *  router, and the interesting half of what they do is this. */
export function searchString(updates: Record<string, string | null>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(updates)) {
    if (value !== null) params.set(key, value);
  }
  const search = params.toString();
  return search ? "?" + search : "";
}

/** Replace the WHOLE query with `updates` — everything not named is dropped,
 *  where `writeParams` below keeps what it was not asked about.
 *
 *  For a move between stages. The stages share a namespace (`prompt` is a
 *  sentence to a text model and a scene to an image one; `steps`, `seed` and
 *  `w`/`h` mean different things to each engine that has them) and each stage
 *  only ever nulls its OWN keys, so a merge-style rewrite carries the
 *  abandoned stage's settings into the new one — where they are read if the
 *  name happens to collide and are dead weight in the URL if it does not.
 *  Naming what to keep, rather than what to clear, is the direction that does
 *  not go stale: a stage that adds a parameter tomorrow is covered. */
export function resetParams(
  updates: Record<string, string | null>,
  mode: WriteMode = "replace",
): void {
  land(location.pathname + searchString(updates), mode);
}

/** Rewrite query params — null deletes. `replace` by default: slider drags
 *  must not stack history entries. The model picker passes `push`. */
export function writeParams(
  updates: Record<string, string | null>,
  mode: WriteMode = "replace",
): void {
  const params = new URLSearchParams(location.search);
  for (const [key, value] of Object.entries(updates)) {
    if (value === null) params.delete(key);
    else params.set(key, value);
  }
  const search = params.toString();
  land(location.pathname + (search ? "?" + search : ""), mode);
}
