import { describe, expect, it } from "bun:test";
import { carries, getSnapshotAppDir, setSnapshotAppDir } from
  "@platform/lib/snapshot-param";

const APP = "/repo/myapp";

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

describe("the app-dir singleton", () => {
  it("round-trips what was last set, defaulting to null", () => {
    setSnapshotAppDir(null);
    expect(getSnapshotAppDir()).toBe(null);
    setSnapshotAppDir(APP);
    expect(getSnapshotAppDir()).toBe(APP);
    setSnapshotAppDir(null);
    expect(getSnapshotAppDir()).toBe(null);
  });
});
