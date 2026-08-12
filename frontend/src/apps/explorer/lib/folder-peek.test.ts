// Which child a folder card peeks at, and how it renders it. The card has a
// budget of ONE live preview, so this ranking decides the single thing a folder
// shows about itself — a wrong answer is a card showing a stray .json where the
// author put a screenshot.
import { expect, test } from "bun:test";
import type { FsEntry } from "@platform/lib/api";

import {
  PREVIEW_IMAGE_NAME,
  bestPeekFile,
  foldProbePick,
  isPreviewImage,
  peekRank,
  peekRankIsUnbeatable,
} from "@apps/explorer/lib/folder-peek";
import type { ProbePick } from "@apps/explorer/lib/folder-peek";

function f(name: string, is_dir = false): FsEntry {
  return { name, is_dir, size: null, mtime: null };
}

test("the three entry-point names rank first, in order, above every other file", () => {
  // The change this ordering exists for: an html used to win outright, so a
  // fused app folder always showed its page rendered live. An authored
  // screenshot is a deliberate statement about what the folder looks like, and
  // beats anything derived; the two conventional entry-point names follow it,
  // and only then does anything else get a turn.
  expect(peekRank(PREVIEW_IMAGE_NAME)).toBeLessThan(peekRank("index.html"));
  expect(peekRank("index.html")).toBeLessThan(peekRank("README.md"));
  expect(peekRank("README.md")).toBeLessThan(peekRank("about.html"));
  expect(peekRank("about.html")).toBeLessThan(peekRank("notes.md"));
  expect(peekRank("notes.md")).toBeLessThan(peekRank("data.json"));
  expect(peekRank("data.json")).toBeLessThan(peekRank("notes.txt"));
});

test("the entry-point names beat their own extension tier, whatever the case", () => {
  // The whole point of naming them: alphabetical tie-breaking used to hand the
  // peek to `about.html` over `index.html` and to `changelog.md` over the
  // readme — a card showing the folder's least representative page.
  expect(peekRank("index.html")).toBeLessThan(peekRank("about.html"));
  expect(peekRank("INDEX.HTML")).toBe(peekRank("index.html"));
  expect(peekRank("readme.md")).toBeLessThan(peekRank("changelog.md"));
  expect(peekRank("README.md")).toBe(peekRank("readme.md"));
  // Only the exact names — a readme in another format is an ordinary .txt.
  expect(peekRank("readme.txt")).toBe(peekRank("notes.txt"));
});

test("only that exact name is the authored preview, case included", () => {
  // A whole-name match, not an extension one: another .png in the folder is
  // just a file. And case-SENSITIVE, so this agrees with app_preview_image on
  // ext4 as well as on a case-folding filesystem — see the module comment.
  expect(peekRank("shot.png")).toBe(peekRank("notes.txt"));
  expect(peekRank("preview.jpg")).toBe(peekRank("notes.txt"));
  expect(peekRank("Preview.png")).toBe(peekRank("notes.txt"));
});

test("the subfolder probe stops at a page, not only at an image", () => {
  // The regression guard for the early exit: `.html` used to be rank 0, so the
  // probe's `rank === 0` break fired for the common folder-of-apps case.
  // Inserting preview.png above it made that break unreachable and turned one
  // listDir into three — the sequential-listing pattern that stalls mounts.
  expect(peekRankIsUnbeatable(peekRank(PREVIEW_IMAGE_NAME))).toBe(true);
  expect(peekRankIsUnbeatable(peekRank("index.html"))).toBe(true);
  expect(peekRankIsUnbeatable(peekRank("about.html"))).toBe(true);
  // A readme outranks a plain html in the PEEK order but must not stop the
  // probe: a readme in the first subfolder would otherwise hide a real app in
  // the second. This is why the stop rule is a set and not a `<=` threshold.
  expect(peekRankIsUnbeatable(peekRank("README.md"))).toBe(false);
  expect(peekRankIsUnbeatable(peekRank("notes.txt"))).toBe(false);
});

test("the peeked child is the best-ranked file, never a directory", () => {
  // `about.html` sorts before `index.html`, and `changelog.md` before the
  // readme — the alphabetical tie-break must not get a say over the names.
  const entries = [
    f("assets", true),
    f("about.html"),
    f("changelog.md"),
    f("data.json"),
    f("index.html"),
    f("preview.png"),
    f("README.md"),
  ];
  expect(bestPeekFile(entries)?.name).toBe("preview.png");
  const drop = (names: string[]) => entries.filter((e) => !names.includes(e.name));
  expect(bestPeekFile(drop(["preview.png"]))?.name).toBe("index.html");
  expect(bestPeekFile(drop(["preview.png", "index.html"]))?.name).toBe("README.md");
  expect(bestPeekFile(drop(["preview.png", "index.html", "README.md"]))?.name).toBe(
    "about.html",
  );
  expect(bestPeekFile([f("only-a-folder", true)])).toBe(null);
});

// The probed-subfolder walk, run over a sequence of finds the way the card's
// effect does: fold each one in, stop when the fold says so.
function walk(finds: string[]): { dir: string; visited: number } | null {
  let best: ProbePick | null = null;
  let visited = 0;
  for (const [i, name] of finds.entries()) {
    visited++;
    const dir = `sub${i}`;
    const folded = foldProbePick(best, { path: `/w/${dir}/${name}`, rank: peekRank(name), dir });
    best = folded.best;
    if (folded.done) break;
  }
  return best && { dir: best.dir, visited };
}

test("the probe stops on the pick that won, not on the candidate it just saw", () => {
  // The regression this fold exists for. A `readme.md` does not stop the walk
  // (it isn't evidence of an app), but it DOES outrank a plain html — so an
  // `about.html` in the second subfolder is unbeatable-enough to break the loop
  // while losing to the readme already held. Stopping on the candidate
  // abandoned the search with the readme, never reaching sub2's index.html.
  expect(walk(["README.md", "about.html", "index.html"])).toEqual({
    dir: "sub2",
    visited: 3,
  });
});

test("the probe still stops the moment it holds something unbeatable", () => {
  // The other half: the early exit is what keeps a folder-of-apps card to ONE
  // listDir instead of three, which on an rclone/NFS target is the difference
  // between a card and a stalled mount.
  expect(walk(["index.html", "preview.png"])).toEqual({ dir: "sub0", visited: 1 });
  expect(walk(["app.html", "index.html"])).toEqual({ dir: "sub0", visited: 1 });
  expect(walk([PREVIEW_IMAGE_NAME, "index.html"])).toEqual({ dir: "sub0", visited: 1 });
  // Nothing stoppable anywhere: the walk runs out, keeping the best it saw.
  expect(walk(["notes.txt", "data.json", "notes.md"])).toEqual({ dir: "sub2", visited: 3 });
  expect(walk([])).toBe(null);
});

test("a probe tie keeps the earlier subfolder", () => {
  // Subfolders are walked in order, so the first is the folder's own
  // alphabetical answer — a later equal find must not steal the front sheet.
  const held: ProbePick = { path: "/w/sub0/notes.md", rank: peekRank("notes.md"), dir: "sub0" };
  const tie: ProbePick = { path: "/w/sub1/other.md", rank: peekRank("other.md"), dir: "sub1" };
  expect(foldProbePick(held, tie).best.dir).toBe("sub0");
});

test("a peeked preview.png is recognised from its full path", () => {
  // The peek travels as an absolute path (it may come from a PROBED
  // subfolder), and it is the basename that decides whether the card renders an
  // <img> or frames a whole embed page.
  expect(isPreviewImage("/w/local/demo/preview.png")).toBe(true);
  expect(isPreviewImage("/w/local/preview.png/index.html")).toBe(false);
  expect(isPreviewImage("/w/local/demo/index.html")).toBe(false);
});
