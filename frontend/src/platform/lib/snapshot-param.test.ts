import { describe, expect, it } from "bun:test";
import {
  carries,
  getResolvedSnapshot,
  getSnapshotAppDir,
  rewriteSnapshotPath,
  setResolvedSnapshot,
} from "@platform/lib/snapshot-param";

const APP = "/repo/myapp";
const DIR = "/cache/key/abc1234";
const SNAP = { sha: "abc1234", dir: DIR, app_dir: APP };

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
