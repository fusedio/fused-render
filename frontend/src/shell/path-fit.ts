// HOW MUCH OF A PATH FITS, and which half of it goes (Akshil, 2026-09-19:
// "start and end of path, rest middle-truncate; show how much path fits in the
// given width").
//
// WHY NOT `text-overflow: ellipsis`. CSS only ever cuts the TAIL, and the tail
// of a path is the half that says WHICH folder this is — `~/Desktop/fused/fu…`
// and `~/Desktop/fused/fus…` are two different checkouts printed identically.
// The start says where on the machine you are, the end says what it is, and the
// middle — the `Desktop/projects/2026/checkouts` segments every path on one
// machine shares — is the part nobody reads. So the middle is what goes.
//
// NO DOM IN HERE. The caller passes `fits`, which is the only thing that knows
// about pixels: this file decides the SHAPE of the answer and the caller
// measures it. That is what makes the whole ladder assertable without a canvas,
// a layout or a browser.

/** The one character that stands in for what was cut. Exported so the callers
 *  that measure, test or strip the result never spell it a second way — `...`
 *  is three glyphs wide where this is one. */
export const ELLIPSIS = "…";

/** Windows writes its separators the other way round; everything below counts
 *  segments, so it has to count them in one alphabet. A trailing separator
 *  names the same folder, and would otherwise leave an EMPTY last segment —
 *  i.e. a tail of "", which is the one string no amount of width can help. */
function normalise(path: string): string {
  const slashed = path.replace(/\\/g, "/");
  return slashed.length > 1 ? slashed.replace(/\/+$/, "") : slashed;
}

/** The longest TAIL of a name that fits behind a leading ellipsis. The last
 *  resort, for a folder whose own name is wider than the box: the END of a name
 *  is what tells two checkouts apart (`…-render` vs `…-server`), so the end is
 *  the end that survives. Never cuts below one character — an answer of `…`
 *  alone says nothing at all, and a box that narrow is a layout bug this
 *  function cannot fix. */
function clipFront(tail: string, fits: (s: string) => boolean): string {
  let cut = tail;
  while (cut.length > 1 && !fits(ELLIPSIS + cut)) cut = cut.slice(1);
  return ELLIPSIS + cut;
}

/**
 * AS MUCH PATH AS FITS, cut out of the MIDDLE.
 *
 * The ladder, widest rung first:
 *   1. the path itself, whenever it fits — nothing is cut that need not be;
 *   2. its first TWO segments, then its last — `~/Desktop/…/fused-render`;
 *   3. its first ONE — `~/…/fused-render`;
 *   4. no head at all — `…/fused-render`;
 *   5. and when even the folder's own name is too wide, that name's tail —
 *      `…used-render`.
 *
 * A LEADING `~` OR `/` COUNTS AS THE FIRST SEGMENT (`~/Desktop`, `/Users`):
 * `~` is a place on the machine, and the empty string before an absolute path's
 * first slash is the root — dropping either would turn "in my home folder" into
 * "somewhere", which is the one thing the head of a path is there to say.
 *
 * Rungs 2 and 3 are SKIPPED for a path of three segments or fewer, because
 * there is nothing between its head and its tail to elide: `~/dev/x` rewritten
 * with a head of two is `~/dev/…/x`, which is longer than what it replaced and
 * hides nothing. Those paths go straight to rung 4 and, failing that, rung 5.
 *
 * @param path  what to shorten — already `~`-shortened by the caller if it is
 *              going to be; this only ever removes characters.
 * @param fits  true when the caller can draw the string in the space it has.
 */
export function fitPathMiddle(path: string, fits: (s: string) => boolean): string {
  if (!path) return path;
  if (fits(path)) return path;
  const parts = normalise(path).split("/");
  const tail = parts[parts.length - 1] ?? "";
  if (!tail) return path;
  // `> 3` is the "has a middle" test: head (1–2) + something elided + tail.
  if (parts.length > 3) {
    for (let head = 2; head >= 1; head--) {
      const candidate = parts.slice(0, head).join("/") + "/" + ELLIPSIS + "/" + tail;
      if (fits(candidate)) return candidate;
    }
  }
  const bare = ELLIPSIS + "/" + tail;
  if (fits(bare)) return bare;
  return clipFront(tail, fits);
}
