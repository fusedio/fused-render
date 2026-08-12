// Which folder is "an app" as far as the explorer's in-page chrome is
// concerned. The rule is not this module's to invent: the SERVER already
// decided it (fused_render/app_listing.py::app_entry) when it listed the folder
// as a card on the /apps hub, and a page that answers differently is how a
// folder becomes an app you cannot open — the card says app, the listing offers
// no "Open as app", and a directory has no top-bar mode switcher to fall back
// on. Every case here is a way the two used to disagree.
import { expect, test } from "bun:test";
import type { FsEntry } from "@platform/lib/api";

import { loneEntryPage } from "@apps/explorer/lib/folder-app";

function f(name: string, is_dir = false): FsEntry {
  return { name, is_dir, size: null, mtime: null };
}

test("a folder with exactly one top-level .html has that page", () => {
  expect(loneEntryPage([f("index.html"), f("style.css"), f("assets", true)])).toBe(
    "index.html",
  );
});

test("a sibling .htm does NOT make the folder ambiguous", () => {
  // The divergence that motivated this module. The row-badging predicate
  // (isAppEntry) treats .htm as html, which is right for "can this file be
  // launched" and wrong here: app_entry counts `.html` only, so the hub lists
  // this folder as an app with index.html as its entry. Counting the .htm made
  // it "two pages, ambiguous" and withheld the button.
  expect(loneEntryPage([f("index.html"), f("legacy.htm")])).toBe("index.html");
});

test("hidden pages are not counted", () => {
  // /api/fs/list returns dotfiles; app_entry skips them. An editor backup or a
  // hidden template beside the real page must not read as a second entry.
  expect(loneEntryPage([f("index.html"), f(".index.html")])).toBe("index.html");
  expect(loneEntryPage([f(".index.html")])).toBe(null);
});

test("the extension match is case-insensitive, like the server's", () => {
  expect(loneEntryPage([f("Index.HTML")])).toBe("Index.HTML");
});

test("zero or several pages is not an app folder", () => {
  expect(loneEntryPage([f("notes.md"), f("data", true)])).toBe(null);
  expect(loneEntryPage([f("index.html"), f("about.html")])).toBe(null);
});

test("a DIRECTORY named like a page is not a page", () => {
  // app_entry's isfile check, restated: an <img>/iframe pointed at a directory
  // renders nothing, and counting it would hide the real lone page.
  expect(loneEntryPage([f("index.html"), f("old.html", true)])).toBe("index.html");
  expect(loneEntryPage([f("index.html", true)])).toBe(null);
});
