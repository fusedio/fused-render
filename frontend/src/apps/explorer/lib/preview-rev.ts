// The CONTENT pane's revision, as decisions rather than as JSX — the same reason
// preview-side.ts is DOM-free next door: what is written here is a set of
// invariants, and an invariant stated only inside a component is one nothing can
// pin.
//
// WHAT THIS IS. Clicking a commit in the `git` sidebar makes the content pane
// beside it render the open file AS OF that commit: read-only, resolved out of the
// object database on read (/api/git/show), nothing written to disk. The sha
// reaches this shell through the runtime's ancestor-window hop
// (window._fusedRevSelected, installed by Preview.tsx), never as a param.
//
// WHY NOT A PARAM — the constraint the whole design is shaped by. A `_rev` on the
// shell's URL would be carried onto the NEXT file (a path change preserves the
// query verbatim), recorded into the file's session sidecar 400ms later and
// replayed on the next bare open (platform/lib/session), and stored in a bookmark
// (platform/lib/bookmarks). A revision is none of those things: it is a way of
// LOOKING at one file for as long as you are looking. So it lives only on the
// iframe's own src — the third param of that kind, after `_file` and `chat_only=1`.
//
// THE THREE INVARIANTS, and the reason `activeRev` exists at all. The selection is
// held as {sha, path} — the sha AND the file it was chosen for — and every read of
// it goes through `activeRev`, which is a DERIVATION and not a cache:
//
//   * gone whenever the sidebar is not showing `git` — closing the sidebar, or
//     switching it to Claude/History, returns the pane to live content;
//   * gone when the open file changes — a sha picked from one file's commit list
//     says nothing about the next file, and this is the exact leak a URL param
//     would have caused;
//   * gone on reload — free, and the whole point of it being state: there is
//     nothing in the URL, the sidecar or a bookmark to restore.
//
// Deriving rather than clearing-by-effect is what makes the first two true BY
// CONSTRUCTION. An effect that clears on a dependency change runs AFTER the paint
// that changed it, so there is one frame in which the pane is framing the previous
// file at a revision chosen for a different one — and a missed dependency is a leak
// that survives indefinitely. A derivation cannot be late and cannot be missed.

// The sha, and the absolute path of the file it was chosen for. The path is what
// makes the second invariant checkable at all: a bare sha in state is indistinguish
// -able from a sha meant for another file.
export interface RevSelection {
  sha: string;
  path: string;
}

// A hex object name, full or abbreviated — the same shape the server's
// /api/git/show accepts and the runtime re-checks before it builds a read URL.
// Validated on the way IN (see `revFromHook`) so a junk value can never become a
// frame src.
const SHA_RE = /^[0-9a-fA-F]{4,64}$/;

export function isSha(value: unknown): value is string {
  return typeof value === "string" && SHA_RE.test(value);
}

// What the hook stores for an incoming report: a selection, or null for "back to
// live". Anything that is not a usable sha reads as null rather than throwing — the
// caller is a window global any same-origin frame can call, so a bad value is a
// no-op, not an exception in someone else's frame.
export function revFromHook(sha: unknown, fsPath: string): RevSelection | null {
  if (!isSha(sha) || !fsPath) return null;
  return { sha, path: fsPath };
}

// THE ONE READ of the selection. Null unless every invariant above holds.
export function activeRev(
  sel: RevSelection | null,
  activeSide: string | null,
  fsPath: string
): string | null {
  if (!sel) return null;
  // The revision belongs to the git companion. Any other sidebar — or none — has
  // no commit list on screen to explain a revision pane, so there must not be one.
  if (activeSide !== "git") return null;
  if (sel.path !== fsPath) return null;
  return sel.sha;
}

// `_rev` onto a content frame's src. Deliberately takes the URL the shell already
// built rather than composing one: `srcFor` owns what a frame's src is (which
// template, which `_file`, `_remote`), and this adds one param to it. Null src
// (the `_listing` sentinel, an unresolved mode) stays null — there is no frame.
export function revSrc(src: string | null, rev: string | null): string | null {
  if (src === null || rev === null) return src;
  return src + "&_rev=" + encodeURIComponent(rev);
}

// The pill's short form. Seven, the same abbreviation the git template's rows and
// `git log --oneline` show, so the pane and the commit list read as the same commit
// rather than as two ids.
export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}
