// Which child a folder card peeks at, and how it renders it. The card has a
// budget of ONE live preview, so this ranking decides the single thing a folder
// shows about itself — a wrong answer is a card showing a stray .json where the
// author put a screenshot.
//
// BookmarkCards pulls the router, which reads `location` at module scope, so the
// stub precedes the (therefore dynamic) import — the same trade
// app-button.test.ts makes rather than carrying a DOM.
import { expect, test } from "bun:test";
import type { FsEntry } from "@platform/lib/api";

(globalThis as { location?: unknown }).location = new URL("http://x/");
const { bestPeekFile, isPreviewImage, peekRank } = await import(
  "@apps/explorer/BookmarkCards"
);

function f(name: string, is_dir = false): FsEntry {
  return { name, is_dir } as FsEntry;
}

test("preview.png outranks every extension, index.html included", () => {
  // The change this ordering exists for: an html used to win outright, so a
  // fused app folder always showed its page rendered live. An authored
  // screenshot is a deliberate statement about what the folder looks like, and
  // beats anything derived.
  expect(peekRank("preview.png")).toBeLessThan(peekRank("index.html"));
  expect(peekRank("index.html")).toBeLessThan(peekRank("README.md"));
  expect(peekRank("README.md")).toBeLessThan(peekRank("data.json"));
  expect(peekRank("data.json")).toBeLessThan(peekRank("notes.txt"));
});

test("only that exact name is the authored preview", () => {
  // A whole-name match, not an extension one: another .png in the folder is
  // just a file, and guessing at it would be worse than showing the page.
  expect(peekRank("shot.png")).toBe(peekRank("notes.txt"));
  expect(peekRank("preview.jpg")).toBe(peekRank("notes.txt"));
});

test("the peeked child is the best-ranked file, never a directory", () => {
  const entries = [f("assets", true), f("data.json"), f("index.html"), f("preview.png")];
  expect(bestPeekFile(entries)?.name).toBe("preview.png");
  expect(bestPeekFile(entries.filter((e) => e.name !== "preview.png"))?.name).toBe(
    "index.html",
  );
  expect(bestPeekFile([f("only-a-folder", true)])).toBe(null);
});

test("a peeked preview.png is recognised from its full path", () => {
  // The peek travels as an absolute path (it may come from a PROBED
  // subfolder), and it is the basename that decides whether the card renders an
  // <img> or frames a whole embed page.
  expect(isPreviewImage("/w/local/demo/preview.png")).toBe(true);
  expect(isPreviewImage("/w/local/preview.png/index.html")).toBe(false);
  expect(isPreviewImage("/w/local/demo/index.html")).toBe(false);
});
