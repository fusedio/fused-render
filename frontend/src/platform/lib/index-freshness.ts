// Which folders the index is no longer allowed to answer for, because THIS
// app changed them after the last scan.
//
// There is no filesystem watcher (index/specs/scan.md), so the index is a
// snapshot: rename a file and the corpus keeps offering the old name and
// cannot offer the new one until the next scan. For out-of-band edits that
// is the documented trade — the index is rebuilt at startup and a scan says
// "indexing…" while it runs. For edits the explorer itself just made it is
// not a trade, it is a visible lie: the user renamed the file in this window
// and search still can't find it.
//
// So mutations issued through this app mark their folder, and the in-folder
// search walks it live instead. Deliberately NOT a rescan trigger: an
// incremental scan of a whole root is minutes of background crawl (the
// startup scheduler debounces it to once per 15 minutes for that reason),
// and re-walking one folder on demand is both cheaper and immediate.
//
// Session-scoped and in-memory on purpose. A reload gets a fresh startup
// scan, which is what actually repairs the index.

// Enough to cover a working session's edits without unbounded growth. Older
// entries fall off the front; if a user really made 256 mutations in one
// session, the earliest folders have almost certainly been rescanned by the
// startup scan of a later window anyway.
const MAX_TRACKED = 256;

// Parent folders of everything mutated this session, oldest first.
let touched: string[] = [];

// Monotonic count of mutations, and the listeners watching for them. The
// search's revalidation deferral (listing/revalidate) reads this: a change the
// USER just made is the one case that must repaint immediately, so it needs a
// signal rather than having to poll `indexMayAnswer`.
let mutations = 0;
const listeners = new Set<() => void>();

/** How many mutations this app has made this session. */
export function fsMutationCount(): number {
  return mutations;
}

/** Subscribe to in-app mutations. Returns an unsubscribe function. */
export function subscribeFsMutations(fn: () => void): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}

function norm(p: string): string {
  const s = String(p || "").replace(/\/+$/, "");
  return s || "/";
}

function parentOf(p: string): string {
  const s = norm(p);
  const cut = s.lastIndexOf("/");
  return cut <= 0 ? "/" : s.slice(0, cut);
}

// `a` is `b` or sits underneath it. Compared segment-wise so /x/proj-old is
// not read as a descendant of /x/proj.
function isAtOrUnder(a: string, b: string): boolean {
  return a === b || a.startsWith(b === "/" ? "/" : b + "/");
}

/** Record that this app changed `path` (created, deleted, renamed, written). */
export function noteFsMutation(path: string): void {
  const p = norm(path);
  if (!p || p === "/") return;
  // BOTH ends matter: the entry's own folder (its listing changed) and the
  // entry itself (a renamed DIRECTORY invalidates everything below it).
  for (const entry of [parentOf(p), p]) {
    const at = touched.indexOf(entry);
    if (at !== -1) touched.splice(at, 1);
    touched.push(entry);
  }
  if (touched.length > MAX_TRACKED) touched = touched.slice(-MAX_TRACKED);
  mutations++;
  for (const fn of listeners) fn();
}

/**
 * Whether the index's corpus for `folder` is still trustworthy.
 *
 * In-folder search is recursive, so the corpus covers the whole subtree: a
 * change anywhere below `folder` invalidates it, and so does a change to an
 * ancestor (a renamed parent moves every path underneath).
 */
export function indexMayAnswer(folder: string): boolean {
  const f = norm(folder);
  return !touched.some((t) => isAtOrUnder(t, f) || isAtOrUnder(f, t));
}

/** Tests only. */
export function resetFsMutations(): void {
  touched = [];
  mutations = 0;
}
