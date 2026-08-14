// Which child a folder card peeks at. The explorer homepage's folder cards fan
// out a few entries as chips and give ONE of them a live preview underneath —
// one per card is the whole embed budget — so this ranking decides the single
// thing a folder shows about itself.
//
// The order, best first: `preview.png`, `index.html`, `readme.md`, then any
// other file by extension tier. Ties break alphabetically, since entries arrive
// sorted.
//
// Pure and DOM-free, in lib/ rather than beside the card component, so the rule
// can be pinned by a test that imports a function instead of a React tree (the
// same reason lib/app-button.ts exists).
import type { FsEntry } from "@platform/lib/api";

// The authored thumbnail, and the same file the /apps hub shows on an app card
// (fused_render/app_listing.PREVIEW_IMAGE_NAME).
//
// Compared EXACTLY, case included, which is the rule the server was made to
// meet rather than the other way round: `os.path.isfile` inherits the
// filesystem's case-folding, so a lowercasing rule here would have agreed with
// the server on macOS and disagreed on ext4 — one folder, two answers about its
// own picture depending on the machine. An exact match is the only rule both
// sides can hold on every filesystem, so `app_preview_image` lists the
// directory to get one.
//
// Matched by whole NAME, not by extension: any other .png in the folder is just
// a file, and would be a poor guess at what the folder is about.
export const PREVIEW_IMAGE_NAME = "preview.png";

const PREVIEW_RANK = 0;

// Conventional entry-point names, ranked ABOVE their own extension tier and
// above every other extension: these three names are the folder introducing
// itself, and one of them is almost always what an author would have picked by
// hand. `index.html` first (the folder is a fused-app — show the app itself),
// then `readme.md` (the folder's story), and only then any other file.
//
// Matched case-INSENSITIVELY, unlike `preview.png` above: `README.md` is at
// least as common as the lowercase spelling, and nothing on the server side
// depends on this list agreeing byte-for-byte with a directory entry, so the
// filesystem-parity argument that forces an exact match there does not apply.
const NAMED_PEEK_ORDER = ["index.html", "index.htm", "readme.md"];
const NAMED_BASE = PREVIEW_RANK + 1;

// Peek priority for everything not named above: an html beats an md beats a
// json beats anything else.
const PEEK_EXT_ORDER = ["html", "htm", "md", "markdown", "json"];
const PEEK_EXT_BASE = NAMED_BASE + NAMED_PEEK_ORDER.length;

// The best rank an ordinary (non-index) page can reach.
export const PAGE_RANK = PEEK_EXT_BASE + PEEK_EXT_ORDER.indexOf("html");

export function peekRank(name: string): number {
  if (name === PREVIEW_IMAGE_NAME) return PREVIEW_RANK;
  const lower = name.toLowerCase();
  const named = NAMED_PEEK_ORDER.indexOf(lower);
  if (named !== -1) return NAMED_BASE + named;
  const dot = lower.lastIndexOf(".");
  const ext = dot === -1 ? "" : lower.slice(dot + 1);
  const idx = PEEK_EXT_ORDER.indexOf(ext);
  return PEEK_EXT_BASE + (idx === -1 ? PEEK_EXT_ORDER.length : idx);
}

// The ranks a probe may stop on: the authored image, an index page, or any
// other html page — everything that answers "there is a fused app in here".
//
// Deliberately NOT a `rank <= PAGE_RANK` threshold, even though that used to be
// the rule: `readme.md` now outranks a plain html in the peek order, and a
// threshold would swallow it. A readme in the FIRST subfolder must not stop the
// probe from finding an actual app in the second — a readme is what a folder
// says about itself, not evidence of a page worth rendering.
const STOP_RANKS = new Set([
  PREVIEW_RANK,
  ...["index.html", "index.htm"].map(peekRank),
  ...["html", "htm"].map((e) => PEEK_EXT_BASE + PEEK_EXT_ORDER.indexOf(e)),
]);

// Whether a probe that found this rank can stop looking. The subfolder probe
// walks up to APP_PROBE_LIMIT children, each one a `listDir` round trip, and a
// folder-of-apps card would otherwise always pay for all of them — on an
// rclone/NFS target that is the sequential-listing pattern that stalls mounts.
//
// It stops at a PAGE, not only at the image rank, and that is the point:
// `.html` used to BE rank 0, so an old `rank === 0` exit fired for the common
// case; inserting `preview.png` above it silently made the exit unreachable
// there. The residual imprecision is accepted deliberately: a later subfolder
// could hold an `index.html` that outranks the plain page we stopped on, and
// paying two more listings to find it is not worth a stalled mount.
export function peekRankIsUnbeatable(rank: number): boolean {
  return STOP_RANKS.has(rank);
}

// One probed subfolder's best file: where it is, how good it is, and which
// subfolder it came from (that subfolder becomes the card's front sheet).
export type ProbePick = { path: string; rank: number; dir: string };

// Fold a probed subfolder's find into the running pick, and say whether the
// walk can stop.
//
// The stop test is on the pick that WON, never on the candidate just examined,
// and the two are not the same question now that a stop rank is a set instead
// of a threshold. Under the old `rank <= PAGE_RANK` rule they could not
// diverge: any rank low enough to stop the walk was also low enough to beat
// whatever was held. With `readme.md` ranked above a plain html but
// deliberately absent from the stop set, an `about.html` in the second
// subfolder is good enough to stop the walk yet not good enough to displace a
// readme from the first — abandoning the search with the readme still held,
// and an `index.html` in the third subfolder never looked at.
//
// Pure and here rather than inline in the effect that calls it, so this rule
// is pinned by a test instead of by a React tree.
export function foldProbePick(
  best: ProbePick | null,
  candidate: ProbePick,
): { best: ProbePick; done: boolean } {
  // Ties keep the incumbent: subfolders are walked in order, so the earlier one
  // is the folder's own alphabetical answer.
  const next = !best || candidate.rank < best.rank ? candidate : best;
  return { best: next, done: peekRankIsUnbeatable(next.rank) };
}

// The file worth peeking at among a folder's teaser entries: best rank wins;
// entries arrive alphabetically sorted, so ties keep the first.
export function bestPeekFile(entries: FsEntry[]): FsEntry | null {
  let best: FsEntry | null = null;
  for (const e of entries) {
    if (e.is_dir) continue;
    if (!best || peekRank(e.name) < peekRank(best.name)) best = e;
  }
  return best;
}

// A peeked file that is the authored image is shown AS an image, not framed in
// an embed iframe: the embed would be a whole shell page load to render one
// <img>, and its own chrome around it. Takes a PATH — the peek may come from a
// probed subfolder, so only the basename is the name.
export function isPreviewImage(fsPath: string): boolean {
  return fsPath.slice(fsPath.lastIndexOf("/") + 1) === PREVIEW_IMAGE_NAME;
}
