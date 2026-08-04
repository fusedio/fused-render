// The pure pieces of the folder Compress action: the Finder-style archive
// naming, the free-name search that keeps a second Compress from 409ing, and
// the submenu the lazy loader hands to ContextMenu. There are no React
// component tests in this repo, so the menu's shape is only testable because
// buildCompressItems lives here rather than inline in Listing.tsx.
import { afterEach, expect, mock, test } from "bun:test";

import type { MenuItem } from "../components/ContextMenu";

// fs-actions reaches the router, which reads `location` at module scope, so the
// stub has to precede the (therefore dynamic) import — same trade as
// fs-clipboard.test.ts: the suite carries no DOM and this is cheaper than one.
(globalThis as { location?: unknown }).location = new URL("http://x/");
const { archiveName, buildCompressItems, freeArchivePath } = await import("./fs-actions");

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

// freeArchivePath goes through api.listDir / statPath, which go through fetch.
function stubFs(entries: string[], truncated = false, existing: string[] = []) {
  globalThis.fetch = mock(async (url: string) => {
    const u = String(url);
    const body = u.startsWith("/api/fs/list")
      ? { entries: entries.map((name) => ({ name, path: "/d/" + name, is_dir: false })), truncated }
      : null;
    if (body) return new Response(JSON.stringify(body), { status: 200 });
    // stat: 200 only for paths we declared as existing-but-past-the-cap
    const path = decodeURIComponent(u.split("path=")[1] || "");
    return existing.includes(path)
      ? new Response(JSON.stringify({ path }), { status: 200 })
      : new Response(JSON.stringify({ error: "no such file" }), { status: 404 });
  }) as unknown as typeof fetch;
}

// ------------------------------------------------------------- archiveName

test("the first archive keeps the folder's own name", () => {
  expect(archiveName("myrepo", 1, ".zip")).toBe("myrepo.zip");
  expect(archiveName("myrepo", 1, ".tar.gz")).toBe("myrepo.tar.gz");
});

test("later archives get Finder's numeric suffix before the extension", () => {
  expect(archiveName("myrepo", 2, ".zip")).toBe("myrepo 2.zip");
  expect(archiveName("myrepo", 7, ".bundle")).toBe("myrepo 7.bundle");
});

test("a dotted folder name is not treated as an extension", () => {
  expect(archiveName("my.app", 2, ".zip")).toBe("my.app 2.zip");
});

// ---------------------------------------------------------- freeArchivePath

test("an unused name is taken as-is", async () => {
  stubFs(["myrepo", "other.txt"]);
  expect(await freeArchivePath("/d", "myrepo", ".zip")).toBe("/d/myrepo.zip");
});

test("a taken name advances to the next free number", async () => {
  stubFs(["myrepo", "myrepo.zip", "myrepo 2.zip"]);
  expect(await freeArchivePath("/d", "myrepo", ".zip")).toBe("/d/myrepo 3.zip");
});

test("each format is numbered independently", async () => {
  stubFs(["repo", "repo.zip"]);
  expect(await freeArchivePath("/d", "repo", ".bundle")).toBe("/d/repo.bundle");
});

test("a truncated listing verifies the candidate with a stat probe", async () => {
  // The colliding name is past the server's listing cap, so only the probe
  // can see it — without which the compress would come back as a bare 409.
  stubFs(["repo"], true, ["/d/repo.zip"]);
  expect(await freeArchivePath("/d", "repo", ".zip")).toBe("/d/repo 2.zip");
});

test("the filesystem root is a valid parent", async () => {
  stubFs(["repo"]);
  expect(await freeArchivePath("/", "repo", ".zip")).toBe("/repo.zip");
});

// -------------------------------------------------------- buildCompressItems

const labels = (items: ReturnType<typeof buildCompressItems>) =>
  items.map((i) => (i === "separator" ? "---" : i.label));

test("a plain folder offers zip only", () => {
  expect(labels(buildCompressItems(false, () => {}))).toEqual(["Compressed (.zip)"]);
});

test("a repo root additionally offers the two git formats after a separator", () => {
  expect(labels(buildCompressItems(true, () => {}))).toEqual([
    "Compressed (.zip)",
    "---",
    "Git bundle (.bundle)",
    "Git archive of HEAD (.tar.gz)",
  ]);
});

test("each entry carries its own onClick with format and extension", () => {
  const picked: Array<[string, string]> = [];
  const items = buildCompressItems(true, (format, ext) => picked.push([format, ext]));
  for (const it of items) if (it !== "separator") it.onClick?.();
  expect(picked).toEqual([
    ["zip", ".zip"],
    ["git-bundle", ".bundle"],
    ["git-archive", ".tar.gz"],
  ]);
});

test("no submenu entry carries a nested submenu (only one level is rendered)", () => {
  for (const it of buildCompressItems(true, () => {})) {
    if (it !== "separator") expect((it as MenuItem).submenu).toBeUndefined();
  }
});
