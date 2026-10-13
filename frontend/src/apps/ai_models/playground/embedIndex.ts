// Indexing the Files mode's list AS IT IS BUILT, so a search is a dot product
// and not a two-minute wait with a grey button.
//
// Before this, every file was embedded at Search time in one request: twelve
// phone photos and two 4K clips took 138 s behind "Searching…" with nothing
// to say what was happening. Now each file is embedded the moment it lands,
// in small batches of one kind (`planBatches`), and the stage shows which
// file is being read. Searching embeds the PHRASE only and scores it against
// the vectors already held (`scoreAgainst`).
//
// Pure functions here, tested; the queue runner that calls the server lives
// in EmbedStage.tsx with the state it drives.
import { mediaKindOf, type MediaKind } from "./embedAttach";

/** One file's place in the index. `vector` is unit length (the server
 *  normalizes), tagged with the model that made it for the provenance label.
 *  A vector from another model is not comparable; that never arises here
 *  because the stage is keyed per model (PlaygroundTab remounts it on a
 *  switch), so an index never outlives the model that built it. */
export type IndexEntry =
  | { state: "pending" }
  | { state: "indexing" }
  | { state: "done"; vector: number[]; model: string }
  | { state: "failed"; error: string };

/** How many of each kind go to the server per request. Small on purpose —
 *  not for the worker (it chunks on its own) but for the PROGRESS LINE: a
 *  request is the unit the page can report on, so "Indexing 7 of 12" moves
 *  once per batch. Video is one per request because one clip is up to 32
 *  frames through the vision tower and takes seconds on its own. */
export const BATCH_SIZE: Record<MediaKind, number> = {
  picture: 4,
  audio: 4,
  video: 1,
  text: 1,
  other: 1,
};

/** The next batches to send: pending paths grouped by kind, each group cut
 *  to its batch size, in the order the files were added. */
export function planBatches(paths: string[], index: Map<string, IndexEntry>): string[][] {
  const byKind = new Map<MediaKind, string[]>();
  for (const path of paths) {
    if (index.get(path)?.state !== "pending") continue;
    const kind = mediaKindOf(path);
    const group = byKind.get(kind) ?? [];
    group.push(path);
    byKind.set(kind, group);
  }
  const batches: string[][] = [];
  for (const [kind, group] of byKind) {
    const size = BATCH_SIZE[kind];
    for (let at = 0; at < group.length; at += size) batches.push(group.slice(at, at + size));
  }
  return batches;
}

/** Which path a server error is ABOUT, when it names one of `paths` — the
 *  route and the worker both name the file in every refusal and decode
 *  failure, so a batch error can be pinned on one row and the rest retried. */
export function blamedPath(message: string, paths: string[]): string | null {
  // Longest first: `/a/clip.mp4` must win over `/a/clip.mp` if both were in
  // the batch (they cannot be, but the rule costs nothing).
  const sorted = [...paths].sort((a, b) => b.length - a.length);
  return sorted.find((path) => message.includes(path)) ?? null;
}

/** The count line: how far along the index is. */
export function indexProgress(paths: string[], index: Map<string, IndexEntry>): {
  total: number;
  done: number;
  failed: number;
  busy: boolean;
} {
  let done = 0;
  let failed = 0;
  let busy = false;
  for (const path of paths) {
    const entry = index.get(path);
    if (!entry || entry.state === "pending" || entry.state === "indexing") busy = true;
    else if (entry.state === "done") done += 1;
    else failed += 1;
  }
  return { total: paths.length, done, failed, busy };
}

export interface Scored {
  path: string;
  score: number;
}

/** Every indexed path scored against `query` (a dot product — both unit
 *  length, so this is the cosine), best first. Failed and unindexed paths are
 *  left out: a row with no vector has no score, and a zero would rank it
 *  rather than omit it. */
export function scoreAgainst(query: number[], paths: string[], index: Map<string, IndexEntry>): Scored[] {
  const scored: Scored[] = [];
  for (const path of paths) {
    const entry = index.get(path);
    if (entry?.state !== "done") continue;
    let sum = 0;
    for (let dim = 0; dim < entry.vector.length; dim += 1) sum += entry.vector[dim] * (query[dim] ?? 0);
    scored.push({ path, score: sum });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored;
}
