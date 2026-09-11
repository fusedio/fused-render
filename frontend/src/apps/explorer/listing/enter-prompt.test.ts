import { describe, expect, test } from "bun:test";
import {
  folderToOpen,
  pathNotFoundMessage,
  resolveFolderToOpen,
} from "@apps/explorer/listing/enter-prompt";

// FINDING 6 (code review, 2026-09-10): `enterPrompt` itself and its own
// describe block here are deleted — SPEC-omnibox-search-affordance.md scope
// item 5 removed its one call site (Listing.tsx's banner), and once
// search-action-rows.ts's `searchAffordance` started calling `folderToOpen`
// directly (finding 2), `enterPrompt` had no callers left anywhere but this
// file. `folderToOpen`'s own behaviour — the folder-naming logic
// `enterPrompt` used to wrap — is covered directly below instead of only
// through the wrapper's old assertions.
describe("folderToOpen", () => {
  test("names the folder before the first glob segment", () => {
    expect(folderToOpen("~/Work/*/*.json")).toBe("~/Work");
  });

  test("a glob directly under a bare tilde names just the tilde", () => {
    expect(folderToOpen("~/*.json")).toBe("~");
  });

  test("an absolute glob path names its directory", () => {
    expect(folderToOpen("/tmp/data/*.csv")).toBe("/tmp/data");
  });

  test("a relative .. prefix names the folder the search will actually walk to", () => {
    expect(folderToOpen("../x/*.json")).toBe("../x");
  });

  test("no glob segment: the last segment is a name pattern, dropped", () => {
    expect(folderToOpen("~/Work/notes")).toBe("~/Work");
  });

  test("nothing left to name after dropping falls back to null", () => {
    expect(folderToOpen("~")).toBeNull();
  });

  test("a bare root also falls back to null", () => {
    expect(folderToOpen("/")).toBeNull();
  });
});

// FINDINGS 3 & 4 (code review, 2026-09-10): `resolveFolderToOpen` cannot
// touch the filesystem, so it now declines (returns null) for the two
// shapes that genuinely need a real walk to answer — a `..` segment, and a
// bare absolute path with no `~` and no drive letter — rather than handing
// `navigate()` a guess that might not even be an fsPath (finding 3's
// literal ".."), or might be one nothing backs on disk (finding 4's
// `/Users` on a machine with no `/Users`). The caller falls back to
// `commitSearch()` for a null, the same fallback `home === undefined`
// already used.
describe("resolveFolderToOpen", () => {
  test("expands a `~`-relative folder against home", () => {
    expect(resolveFolderToOpen("~/Work/*/*.json", "/home/iamsdas")).toBe(
      "/home/iamsdas/Work",
    );
  });

  test("stays null while home hasn't resolved yet", () => {
    expect(resolveFolderToOpen("~/Work/*.json", undefined)).toBeNull();
  });

  test("declines a `..`-escaping query — there is no fsPath to climb it from", () => {
    expect(resolveFolderToOpen("../*.md", "/home/iamsdas")).toBeNull();
    expect(resolveFolderToOpen("a/../b/*.json", "/home/iamsdas")).toBeNull();
  });

  test("declines a bare absolute guess — no `~`, no drive letter, nothing to verify it against", () => {
    expect(resolveFolderToOpen("/Users/*/notes.md", "/home/iamsdas")).toBeNull();
    expect(resolveFolderToOpen("/tmp/data/*.csv", "/home/iamsdas")).toBeNull();
  });

  test("still resolves a Windows drive-letter folder unchanged — no ambiguity to decline over", () => {
    expect(resolveFolderToOpen("C:/data/*.csv", "/home/iamsdas")).toBe("C:/data");
  });
});

// Finding 3 (code review): the refusal message for a COMMITTED path-shaped
// query that resolved to nothing — a report, not another invitation to
// press Enter.
describe("pathNotFoundMessage", () => {
  test("names the last segment, the same way a resolved address is named elsewhere", () => {
    expect(pathNotFoundMessage("/nope/here")).toBe("No such file or folder: here");
  });

  test("strips a trailing slash before taking the last segment", () => {
    expect(pathNotFoundMessage("/nope/here/")).toBe("No such file or folder: here");
  });

  test("a home-relative query names its own last segment", () => {
    expect(pathNotFoundMessage("~/Work/nope")).toBe("No such file or folder: nope");
  });
});
