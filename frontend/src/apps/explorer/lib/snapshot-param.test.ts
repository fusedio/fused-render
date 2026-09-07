import { describe, expect, it } from "bun:test";
import {
  carries,
  getResolvedSnapshot,
  getSnapshotAppDir,
  isSha,
  rewriteSnapshotPath,
  setResolvedSnapshot,
} from "@apps/explorer/lib/snapshot-param";

const SHA = "1f0c3a9e2b7d4c5f6a8b9c0d1e2f3a4b5c6d7e8f";
const APP = "/repo/myapp";
const SNAP = { sha: SHA, dir: "/cache/key/" + SHA, app_dir: APP };

// The carry table itself is pinned in platform/lib/snapshot-param.test.ts,
// which owns the rule; this file only has to prove the explorer-facing
// re-export is wired to the same functions, plus `isSha`, which is this
// module's own (ported from preview-rev.ts).

describe("isSha", () => {
  it("takes a hex object name, full or abbreviated", () => {
    expect(isSha(SHA)).toBe(true);
    expect(isSha("1f0c3a9")).toBe(true);
  });

  // Ported from preview-rev.test.ts's junk-value case: the same shape must
  // refuse the same junk, since both modules validate a value bound for the
  // same server endpoint's `sha` argv position.
  it("reads anything that is not a sha as junk", () => {
    for (const bad of [null, undefined, "", "HEAD", "HEAD~2", "../etc/passwd",
                       "1f0", "zzzz", 12345, {}, "1f0c3a9 --upload-pack=x"]) {
      expect(isSha(bad)).toBe(false);
    }
  });
});

describe("the re-exported carry rule and resolved-snapshot singleton", () => {
  it("carries inside the app folder and drops outside it", () => {
    expect(carries(APP, APP + "/sub/file.py")).toBe(true);
    expect(carries(APP, "/repo/otherapp")).toBe(false);
  });

  it("round-trips the resolved-snapshot singleton", () => {
    setResolvedSnapshot(SNAP);
    expect(getResolvedSnapshot()).toEqual(SNAP);
    expect(getSnapshotAppDir()).toBe(APP);
    setResolvedSnapshot(null);
    expect(getResolvedSnapshot()).toBe(null);
    expect(getSnapshotAppDir()).toBe(null);
  });

  it("rewrites a path via the same rule the runtime applies", () => {
    setResolvedSnapshot(SNAP);
    expect(rewriteSnapshotPath(APP + "/reader.py")).toBe(SNAP.dir + "/reader.py");
    expect(rewriteSnapshotPath("/repo/otherapp/x")).toBe("/repo/otherapp/x");
    setResolvedSnapshot(null);
  });
});
