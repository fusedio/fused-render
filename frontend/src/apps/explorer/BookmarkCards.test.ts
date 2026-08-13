// Where a HOME page folder card goes — the card's half of D269, which is the
// half that is allowed to know things the shared entry rule must not.
//
// `lib/app-entry.ts` answers "which page IS this folder", byte-for-byte as
// `templates/shared/app_entry.py::entry_html` answers it, and that parity is
// asserted from both sides (tests/test_shared_app_entry.py). The two guards
// below are NOT that question — they are the card asking whether it may act on
// the answer at all — so they live here and the shared rule stays untouched.
import { expect, test } from "bun:test";

import type { FsEntry, ListResult } from "@platform/lib/api";

// BookmarkCards pulls in router.ts (via urlForFsPath and the app-entry rule's
// own graph), which reads `location` at MODULE scope, and navigate() reaches
// history/window. bun's test runtime has no DOM — same stub, same `??=`, and
// the same dynamic import as platform/lib/appEntry.test.ts, which documents why.
(globalThis as { location?: unknown }).location ??= {
  pathname: "/",
  search: "",
  href: "http://localhost/",
};
(globalThis as { history?: unknown }).history ??= {
  state: null,
  pushState() {},
  replaceState() {},
};
(globalThis as { window?: unknown }).window ??= {
  dispatchEvent() {},
  setTimeout: globalThis.setTimeout.bind(globalThis),
  clearTimeout: globalThis.clearTimeout.bind(globalThis),
};

const { folderCardTarget } = await import("./BookmarkCards");

function entry(name: string, over: Partial<FsEntry> = {}): FsEntry {
  return { name, is_dir: false, size: null, mtime: null, ...over };
}

function listing(entries: FsEntry[], over: Partial<ListResult> = {}): ListResult {
  return { path: "/w/repo", entries, ...over };
}

test("an app folder's card opens its page", () => {
  // The rule itself, seen through the card: unchanged by either guard below.
  expect(folderCardTarget("/w/repo", listing([entry("index.html"), entry("notes.md")]))).toEqual({
    path: "/w/repo/index.html",
    isDir: false,
  });
});

test("no listing in hand is the folder", () => {
  expect(folderCardTarget("/w/repo", null)).toEqual({ path: "/w/repo", isDir: true });
});

// ------------------------------------------------- guard 1: gitignored pages

test("a gitignored page is not a destination", () => {
  // The Repos tab is exactly where the server populates `ignored`, and a repo
  // whose only top-level page is a generated `coverage.html` is the ordinary
  // case, not a corner one. The card never DREW that file — teaserEntries drops
  // ignored entries from the chips and the peek — so opening it would send the
  // user into a build artifact they were shown no sign of, and take the repo's
  // own listing off the homepage entirely.
  expect(
    folderCardTarget("/w/repo", listing([entry("coverage.html", { ignored: true }), entry("src.py")])),
  ).toEqual({ path: "/w/repo", isDir: true });
});

test("a gitignored page does not outrank a real one", () => {
  // Why the ignored entries are FILTERED OUT before the rule runs rather than
  // the resolved answer being vetoed after it: `coverage.html` sorts first, so
  // a veto would drop this folder to its listing even though `page.html` is a
  // perfectly good page the card DID draw.
  expect(
    folderCardTarget(
      "/w/repo",
      listing([entry("coverage.html", { ignored: true }), entry("page.html")]),
    ),
  ).toEqual({ path: "/w/repo/page.html", isDir: false });
  // Same for the index.html shortcut — an ignored index is not the folder's
  // face, and the first real page in name order is.
  expect(
    folderCardTarget(
      "/w/repo",
      listing([entry("index.html", { ignored: true }), entry("page.html")]),
    ),
  ).toEqual({ path: "/w/repo/page.html", isDir: false });
});

test("ignored: false and an absent flag both count as real", () => {
  // `ignored` is optional on the wire (a folder outside any repo, an older
  // server), and an absent flag must not read as "hide this".
  expect(folderCardTarget("/w/repo", listing([entry("index.html", { ignored: false })]))).toEqual({
    path: "/w/repo/index.html",
    isDir: false,
  });
});

// --------------------------------------------------- guard 2: partial pages

test("a truncated listing resolves nothing", () => {
  // The shell sees one PAGE of the directory; `entry_html` sees all of it via
  // os.listdir. A folder that truncates before `index.html` in name order would
  // have the card pick the first page on the partial listing while every other
  // surface picks `index.html` — one folder, two answers, and here it decides a
  // NAVIGATION rather than a preview. So a partial listing answers the folder,
  // which is both the safe fallback and the pre-D269 behaviour.
  expect(
    folderCardTarget("/w/repo", listing([entry("aaa.html")], { truncated: true })),
  ).toEqual({ path: "/w/repo", isDir: true });
  // Even when the page in hand looks like the certain answer: `index.html` wins
  // among what we can SEE, and what we cannot see is the whole problem.
  expect(
    folderCardTarget("/w/repo", listing([entry("index.html")], { truncated: true })),
  ).toEqual({ path: "/w/repo", isDir: true });
});

test("a complete listing is not held back by the optional flag", () => {
  // `truncated` is omitted by older servers and false on every local folder the
  // homepage lists — neither may read as "partial".
  expect(folderCardTarget("/w/repo", listing([entry("index.html")], { truncated: false }))).toEqual({
    path: "/w/repo/index.html",
    isDir: false,
  });
});
