import { describe, expect, it } from "bun:test";
import {
  carriedSnapshotParam,
  carries,
  getResolvedSnapshot,
  getSnapshotAppDir,
  isSha,
  rewritePathAgainst,
  rewriteSnapshotPath,
  setResolvedSnapshot,
  shortSha,
  snapshotFrameSrc,
  snapshotListing,
  snapshotSrc,
} from "@platform/lib/snapshot-param";

const APP = "/repo/myapp";
const DIR = "/cache/key/abc1234";
const SNAP = { sha: "abc1234", dir: DIR, app_dir: APP };
const SHA = "1f0c3a9e2b7d4c5f6a8b9c0d1e2f3a4b5c6d7e8f";

describe("carries — the app-folder-scoped carry table", () => {
  it("carries a hop to the same app folder", () => {
    expect(carries(APP, APP)).toBe(true);
  });

  it("carries a hop into a subfolder of the app", () => {
    expect(carries(APP, APP + "/sub/reader.py")).toBe(true);
  });

  it("carries a hop to a sibling FILE inside the app folder", () => {
    expect(carries(APP, APP + "/index.html")).toBe(true);
  });

  it("drops a hop out of the app folder (breadcrumb up)", () => {
    expect(carries(APP, "/repo")).toBe(false);
  });

  it("drops a hop to a different, sibling app folder", () => {
    expect(carries(APP, "/repo/otherapp")).toBe(false);
  });

  it("does not treat a same-prefix sibling as inside the app", () => {
    expect(carries(APP, "/repo/myapp-notes/file.txt")).toBe(false);
  });

  it("never invents a carry where nothing has resolved an app folder yet", () => {
    expect(carries(null, APP + "/index.html")).toBe(false);
  });
});

describe("the resolved-snapshot singleton", () => {
  it("round-trips what was last set, defaulting to null", () => {
    setResolvedSnapshot(null);
    expect(getResolvedSnapshot()).toBe(null);
    expect(getSnapshotAppDir()).toBe(null);
    setResolvedSnapshot(SNAP);
    expect(getResolvedSnapshot()).toEqual(SNAP);
    expect(getSnapshotAppDir()).toBe(APP);
    setResolvedSnapshot(null);
    expect(getResolvedSnapshot()).toBe(null);
    expect(getSnapshotAppDir()).toBe(null);
  });
});

describe("rewriteSnapshotPath — mirrors static/runtime.js's rewritePath", () => {
  it("rewrites a path at or under the app folder", () => {
    setResolvedSnapshot(SNAP);
    expect(rewriteSnapshotPath(APP)).toBe(DIR);
    expect(rewriteSnapshotPath(APP + "/reader.py")).toBe(DIR + "/reader.py");
    expect(rewriteSnapshotPath(APP + "/sub/data.parquet")).toBe(DIR + "/sub/data.parquet");
    setResolvedSnapshot(null);
  });

  it("leaves a path outside the app folder alone", () => {
    setResolvedSnapshot(SNAP);
    expect(rewriteSnapshotPath("/repo/otherapp/file.txt")).toBe("/repo/otherapp/file.txt");
    // Same-prefix sibling, not inside the app.
    expect(rewriteSnapshotPath("/repo/myapp-notes/file.txt")).toBe("/repo/myapp-notes/file.txt");
    setResolvedSnapshot(null);
  });

  it("leaves a relative path alone", () => {
    setResolvedSnapshot(SNAP);
    expect(rewriteSnapshotPath("./reader.py")).toBe("./reader.py");
    setResolvedSnapshot(null);
  });

  it("rewrites nothing with no active snapshot", () => {
    setResolvedSnapshot(null);
    expect(rewriteSnapshotPath(APP + "/reader.py")).toBe(APP + "/reader.py");
  });
});

describe("rewritePathAgainst — takes a snapshot explicitly, not the singleton", () => {
  it("rewrites and passes through exactly like rewriteSnapshotPath, given the same snap", () => {
    expect(rewritePathAgainst(SNAP, APP)).toBe(DIR);
    expect(rewritePathAgainst(SNAP, APP + "/reader.py")).toBe(DIR + "/reader.py");
    expect(rewritePathAgainst(SNAP, "/repo/otherapp/file.txt")).toBe("/repo/otherapp/file.txt");
    expect(rewritePathAgainst(null, APP + "/reader.py")).toBe(APP + "/reader.py");
  });

  it(
    "THE regression for finding [1]: diverges from rewriteSnapshotPath when the " +
      "singleton holds a DIFFERENT pane's resolution",
    () => {
      // Two apps in one repo share shas — the singleton (written by
      // whichever pane resolved last) holds appB's answer, but THIS
      // caller's own local resolution (`snap`) is for appA.
      const otherPaneSnap = { sha: "abc1234", dir: "/cache/key/abc1234b", app_dir: "/repo/appB" };
      setResolvedSnapshot(otherPaneSnap);
      const ownSnap = { sha: "abc1234", dir: "/cache/key/abc1234a", app_dir: "/repo/appA" };
      const target = "/repo/appA/index.html";

      // The singleton-reading convenience finds no prefix match for appA
      // under appB's app_dir — this is the bug: a caller that blindly used
      // `rewriteSnapshotPath` here would render the LIVE file.
      expect(rewriteSnapshotPath(target)).toBe(target);
      // Passing the caller's OWN resolution explicitly gets the right answer
      // regardless of what the singleton currently holds.
      expect(rewritePathAgainst(ownSnap, target)).toBe("/cache/key/abc1234a/index.html");
      setResolvedSnapshot(null);
    }
  );
});

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

describe("shortSha", () => {
  it("takes the same seven-character abbreviation the git template shows", () => {
    expect(shortSha(SHA)).toBe("1f0c3a9");
  });
});

describe("snapshotSrc", () => {
  it("appends _snapshot to a real src", () => {
    expect(snapshotSrc("/render?path=x", "abc1234")).toBe("/render?path=x&_snapshot=abc1234");
  });

  it("passes a null src or a null sha through unchanged", () => {
    expect(snapshotSrc(null, "abc1234")).toBe(null);
    expect(snapshotSrc("/render?path=x", null)).toBe("/render?path=x");
  });
});

describe("snapshotFrameSrc — the shared frame-src composer (findings 1/2/4)", () => {
  it("live (no sha, no snap): rewrites nothing, appends no snapshot params", () => {
    expect(snapshotFrameSrc({ snap: null, sha: null, path: "/repo/myapp/index.html" })).toBe(
      "/render?path=" + encodeURIComponent("/repo/myapp/index.html")
    );
  });

  it(
    "resolved: rewrites `path`, AND appends all three params — the whole " +
      "defect this helper closes is one happening without the other",
    () => {
      const src = snapshotFrameSrc({ snap: SNAP, sha: SNAP.sha, path: APP + "/index.html" });
      expect(src).not.toBeNull();
      const u = new URL(src as string, "http://x");
      expect(u.searchParams.get("path")).toBe(DIR + "/index.html");
      expect(u.searchParams.get("_snapshot")).toBe(SNAP.sha);
      expect(u.searchParams.get("_snapshot_dir")).toBe(DIR);
      expect(u.searchParams.get("_snapshot_app")).toBe(APP);
    }
  );

  it("a path already under the extracted tree (not the app folder) is left unchanged by the rewrite", () => {
    // AppPage.tsx's Overview passes `snap.entry` itself — already resolved by
    // the server against the extracted tree — not the live entry path.
    const extractedEntry = DIR + "/main.html";
    const src = snapshotFrameSrc({ snap: SNAP, sha: SNAP.sha, path: extractedEntry });
    const u = new URL(src as string, "http://x");
    expect(u.searchParams.get("path")).toBe(extractedEntry);
  });

  it("pending (sha claimed, not yet resolved) returns null — no frame, not a live one", () => {
    expect(snapshotFrameSrc({ snap: null, sha: SNAP.sha, path: APP + "/index.html" })).toBeNull();
  });

  it(
    "THE regression for finding 4 (second round): `rewritePath: false` leaves `path` " +
      "untouched even when it DOES sit under `snap.app_dir` — a template caller's own path, " +
      "never the subject being previewed, must never be swapped onto the extracted tree",
    () => {
      const templatePathUnderApp = APP + "/vendored-template.html";
      const src = snapshotFrameSrc({
        snap: SNAP,
        sha: SNAP.sha,
        path: templatePathUnderApp,
        rewritePath: false,
        extra: "&_file=" + encodeURIComponent(APP + "/data.csv"),
      });
      const u = new URL(src as string, "http://x");
      // Without `rewritePath: false` this would come back as
      // `DIR + "/vendored-template.html"` — the exact defect finding 4 named.
      expect(u.searchParams.get("path")).toBe(templatePathUnderApp);
      // The three snapshot params still ride along regardless — the point of
      // `rewritePath: false` is narrowly "don't touch `path`", not "treat
      // this as a live, unsnapshotted frame".
      expect(u.searchParams.get("_snapshot")).toBe(SNAP.sha);
      expect(u.searchParams.get("_snapshot_dir")).toBe(DIR);
      expect(u.searchParams.get("_snapshot_app")).toBe(APP);
    }
  );

  it("carries `extra` between the path and the snapshot params", () => {
    const src = snapshotFrameSrc({
      snap: null,
      sha: null,
      path: "/templates/csv/template.html",
      extra: "&_file=" + encodeURIComponent(APP + "/data.csv"),
    });
    const u = new URL(src as string, "http://x");
    expect(u.searchParams.get("_file")).toBe(APP + "/data.csv");
  });
});

describe("carriedSnapshotParam — item 4, round 4", () => {
  it("carries the sha when the git companion's target sits inside the snapshotted app", () => {
    expect(carriedSnapshotParam("git", SNAP, APP)).toBe("&_snapshot=abc1234");
    expect(carriedSnapshotParam("git", SNAP, APP + "/sub")).toBe("&_snapshot=abc1234");
  });

  it("carries nothing once the target leaves the snapshotted app folder", () => {
    expect(carriedSnapshotParam("git", SNAP, "/repo/otherapp")).toBe("");
  });

  it("carries nothing with no active resolution", () => {
    expect(carriedSnapshotParam("git", null, APP)).toBe("");
  });

  it("carries nothing for a companion other than git (mcp has no previewed concept)", () => {
    expect(carriedSnapshotParam("mcp", SNAP, APP)).toBe("");
  });

  it("encodes the sha the same way every other param here does", () => {
    const snap = { sha: "a b&c", dir: DIR, app_dir: APP };
    expect(carriedSnapshotParam("git", snap, APP)).toBe(
      "&_snapshot=" + encodeURIComponent("a b&c")
    );
  });
});

describe("snapshotListing", () => {
  it("is not in-snapshot with no active resolution", () => {
    setResolvedSnapshot(null);
    expect(snapshotListing(APP)).toEqual({ inSnapshot: false, listPath: APP });
  });

  it("rewrites the listing target when the folder is inside the app", () => {
    setResolvedSnapshot(SNAP);
    expect(snapshotListing(APP)).toEqual({ inSnapshot: true, listPath: DIR });
    expect(snapshotListing(APP + "/sub")).toEqual({
      inSnapshot: true,
      listPath: DIR + "/sub",
    });
    setResolvedSnapshot(null);
  });

  it("passes through unrewritten when the folder is outside the app", () => {
    setResolvedSnapshot(SNAP);
    expect(snapshotListing("/repo/otherapp")).toEqual({
      inSnapshot: false,
      listPath: "/repo/otherapp",
    });
    expect(snapshotListing("/repo/myapp-notes")).toEqual({
      inSnapshot: false,
      listPath: "/repo/myapp-notes",
    });
    setResolvedSnapshot(null);
  });
});
