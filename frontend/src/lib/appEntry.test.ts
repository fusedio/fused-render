// Entry resolution for app cards (D205). The rules that matter here are the
// ones that decide which of three thumbnail routes a card takes and what
// clicking it opens — a wrong answer is either a blank card or a navigation to
// a folder full of UUIDs, and neither is visible to a typecheck.
import { expect, test } from "bun:test";

import type { AppInfo } from "./api";

// appEntry pulls `navigate` from router.ts, which reads `location` at MODULE
// scope (IS_EMBED) — and bun's test runtime has no DOM. A static import is
// hoisted above any shim, so the stub goes in first and the module comes in
// dynamically after it. (toast.test.ts shims `window` the same way but can
// import statically: it only touches it at call time.)
(globalThis as { location?: unknown }).location ??= { pathname: "/" };
const { entryOf, extLabel, isImageEntry, rawUrl } = await import("./appEntry");

function app(over: Partial<AppInfo>): AppInfo {
  return {
    name: "a",
    tag: "local",
    path: "/w/a",
    entry_html: null,
    title: null,
    ...over,
  };
}

test("entryOf prefers entry and falls back to entry_html", () => {
  expect(entryOf(app({ entry: "/w/a/fig.png", entry_html: null }))).toBe("/w/a/fig.png");
  // A backend that predates `entry` sends only entry_html; the cards must not
  // go blank against it.
  expect(entryOf(app({ entry_html: "/w/a/index.html" }))).toBe("/w/a/index.html");
  expect(entryOf(app({}))).toBe(null);
  // Explicitly null (an artifact dir we could not read) is not "absent".
  expect(entryOf(app({ entry: null, entry_html: null }))).toBe(null);
});

test("isImageEntry recognises the types an <img> can paint", () => {
  for (const path of ["/a/f.png", "/a/f.JPG", "/a/f.jpeg", "/a/f.svg", "/a/f.webp"]) {
    expect(isImageEntry(path)).toBe(true);
  }
  // A CSV table and an HTML page both have their own route; neither is an image.
  for (const path of ["/a/f.csv", "/a/f.html", "/a/f.parquet", "/a/png", null]) {
    expect(isImageEntry(path)).toBe(false);
  }
});

test("extLabel names what a thumbnail-less card holds", () => {
  expect(extLabel("/a/u/v6f4b965a_overture_coverage_matrix.csv")).toBe("CSV");
  expect(extLabel("/a/u/v6f4b965a_two.parts.here.tsv")).toBe("TSV");
  // Nothing usable to label with — the card keeps its monogram instead.
  expect(extLabel("/a/u/v6f4b965a_dataset")).toBe(null);
  expect(extLabel("/a/u/.gitignore")).toBe(null); // a dotfile's name is not its type
  // The everyday long ones in this app must not be the ones that get dropped.
  expect(extLabel("/a/u/tiles.parquet")).toBe("PARQUET");
  expect(extLabel("/a/u/shapes.geojson")).toBe("GEOJSON");
  expect(extLabel("/a/u/notes.markdownish")).toBe(null); // past the ceiling
  expect(extLabel(null)).toBe(null);
  // A dot in a parent directory must not be mistaken for the file's own.
  expect(extLabel("/Users/i/.claude-science/orgs/26f4/artifacts/p/u/report")).toBe(null);
});

test("rawUrl encodes the path as a query parameter", () => {
  // Claude Science artifact paths carry UUID dirs and saved names that may hold
  // spaces, & and + — all of which must survive as path characters.
  expect(rawUrl("/Users/i/.claude-science/orgs/26f4/artifacts/p/u/v6f4_a b&c+d.png")).toBe(
    "/api/fs/raw?path=%2FUsers%2Fi%2F.claude-science%2Forgs%2F26f4%2Fartifacts%2Fp%2Fu%2Fv6f4_a%20b%26c%2Bd.png",
  );
});
