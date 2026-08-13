import { describe, expect, it } from "bun:test";
import {
  activeRev,
  isSha,
  revFromHook,
  revSrc,
  shortSha,
  type RevSelection,
} from "@apps/explorer/lib/preview-rev";

const SHA = "1f0c3a9e2b7d4c5f6a8b9c0d1e2f3a4b5c6d7e8f";
const OTHER = "abcdef1234567890abcdef1234567890abcdef12";
const FILE = "/repo/pkg/mod.py";

const sel = (sha = SHA, path = FILE): RevSelection => ({ sha, path });

describe("activeRev — the three clearing invariants", () => {
  // The base case the invariants are exceptions to: the git sidebar is open on
  // the file the sha was picked for, so the pane really is a revision pane.
  it("resolves while the git sidebar is open on the same file", () => {
    expect(activeRev(sel(), "git", FILE)).toBe(SHA);
  });

  // INVARIANT 1 — the revision belongs to the git companion and to nothing else.
  // Closing the sidebar and switching it to another companion are the SAME event
  // as far as the content pane is concerned: there is no longer a commit list on
  // screen to explain why the pane is not the live file.
  it("is gone when the sidebar is closed", () => {
    expect(activeRev(sel(), null, FILE)).toBe(null);
  });

  it("is gone when the sidebar switches to another companion", () => {
    for (const side of ["claude", "notes"]) {
      expect(activeRev(sel(), side, FILE)).toBe(null);
    }
  });

  // INVARIANT 2 — and this is the one a URL param could not have given us. The
  // shell preserves the query across a path change, so a `_rev` would have been
  // carried from this file onto the next one; the selection is stamped with the
  // file it was made for, and a mismatch does not resolve.
  it("is gone the moment the open file is a different one", () => {
    expect(activeRev(sel(SHA, "/repo/pkg/mod.py"), "git", "/repo/pkg/other.py")).toBe(null);
  });

  // INVARIANT 3 — reload. Nothing to assert about a URL because there is nothing
  // in one: the selection starts as null, which is the state a fresh mount has.
  it("starts absent, so a reload shows live content", () => {
    expect(activeRev(null, "git", FILE)).toBe(null);
  });

  // Every invariant is a DERIVATION, so it cannot be one paint late. Stated here
  // as the property that matters: no order of arguments makes a selection resolve
  // for a file or a side it was not made for.
  it("never resolves outside its own (side, file) pair", () => {
    const cases: Array<[string | null, string]> = [
      ["git", FILE],
      ["git", "/repo/elsewhere.py"],
      ["claude", FILE],
      [null, FILE],
    ];
    for (const [side, path] of cases) {
      const got = activeRev(sel(), side, path);
      expect(got).toBe(side === "git" && path === FILE ? SHA : null);
    }
  });
});

describe("revFromHook", () => {
  // The hook is a window global any same-origin frame can call, so a bad value
  // reads as "live" rather than throwing inside someone else's frame.
  it("takes a hex object name, full or abbreviated", () => {
    expect(revFromHook(SHA, FILE)).toEqual({ sha: SHA, path: FILE });
    expect(revFromHook("1f0c3a9", FILE)).toEqual({ sha: "1f0c3a9", path: FILE });
  });

  it("reads anything that is not a sha as no revision", () => {
    for (const bad of [null, undefined, "", "HEAD", "HEAD~2", "../etc/passwd",
                       "1f0", "zzzz", 12345, {}, "1f0c3a9 --upload-pack=x"]) {
      expect(revFromHook(bad, FILE)).toBe(null);
    }
    expect(isSha("HEAD")).toBe(false);
    expect(isSha(SHA)).toBe(true);
  });

  it("has no selection without a file to attach it to", () => {
    expect(revFromHook(SHA, "")).toBe(null);
  });
});

describe("revSrc", () => {
  const base = "/render?path=%2Ftpl%2Fcode%2Ftemplate.html&_file=%2Frepo%2Fa.py";

  it("adds _rev to a frame src", () => {
    expect(revSrc(base, SHA)).toBe(base + "&_rev=" + SHA);
  });

  it("leaves the src untouched with no revision", () => {
    expect(revSrc(base, null)).toBe(base);
  });

  // The `_listing` sentinel and an unresolved mode have no frame at all, and a
  // revision must not invent one.
  it("keeps a null src null", () => {
    expect(revSrc(null, SHA)).toBe(null);
    expect(revSrc(null, null)).toBe(null);
  });
});

describe("shortSha", () => {
  // The same abbreviation the git sidebar's rows and `git log --oneline` show, so
  // the badge and the row read as one commit.
  it("is seven characters", () => {
    expect(shortSha(SHA)).toBe("1f0c3a9");
    expect(shortSha(OTHER)).toBe("abcdef1");
  });
});
