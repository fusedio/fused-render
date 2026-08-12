// Which child a folder card peeks at. The explorer homepage's folder cards fan
// out a few entries as chips and give ONE of them a live preview underneath —
// one per card is the whole embed budget — so this ranking decides the single
// thing a folder shows about itself.
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

// Peek priority for everything else: an html (the folder is a fused-app — show
// the app itself) beats an md (the folder's story) beats a json beats anything
// else.
const PEEK_EXT_ORDER = ["html", "htm", "md", "markdown", "json"];

// The best rank an ordinary page can reach — `preview.png` alone outranks it.
// Named because the subfolder probe stops here: see `peekRankIsUnbeatable`.
export const PAGE_RANK = 1;

export function peekRank(name: string): number {
  // Rank 0 is reserved for the authored image; the extension tiers start at 1.
  if (name === PREVIEW_IMAGE_NAME) return 0;
  const dot = name.lastIndexOf(".");
  const ext = dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
  const idx = PEEK_EXT_ORDER.indexOf(ext);
  return PAGE_RANK + (idx === -1 ? PEEK_EXT_ORDER.length : idx);
}

// Whether a probe that found this rank can stop looking. The subfolder probe
// walks up to APP_PROBE_LIMIT children, each one a `listDir` round trip, and a
// folder-of-apps card would otherwise always pay for all of them — on an
// rclone/NFS target that is the sequential-listing pattern that stalls mounts.
//
// It stops at a PAGE, not only at rank 0, and that is the point: `.html` used to
// BE rank 0, so the old `rank === 0` exit fired for the common case; inserting
// `preview.png` above it silently made the exit unreachable there. What the
// probe is looking for is "a fused app in here", an html answers that, and the
// only thing that could outrank one is an authored image in an EARLIER
// subfolder — which the probe would already have seen, since it walks in order.
export function peekRankIsUnbeatable(rank: number): boolean {
  return rank <= PAGE_RANK;
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
