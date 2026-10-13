import { describe, expect, it } from "bun:test";

import { blamedPath, indexProgress, plainError, planBatches, scoreAgainst, type IndexEntry } from "./embedIndex";

const idx = (entries: Record<string, IndexEntry>) => new Map(Object.entries(entries));
const pending: IndexEntry = { state: "pending" };

describe("planBatches groups pending files by kind and cuts each to its batch size", () => {
  it("keeps add order inside a kind and sends videos one at a time", () => {
    const paths = ["/a.png", "/v1.mp4", "/s1.wav", "/b.png", "/c.png", "/d.png", "/e.png", "/v2.mov"];
    const index = idx(Object.fromEntries(paths.map((p) => [p, pending])));
    expect(planBatches(paths, index)).toEqual([
      ["/a.png", "/b.png", "/c.png", "/d.png"],
      ["/e.png"],
      ["/v1.mp4"],
      ["/v2.mov"],
      ["/s1.wav"],
    ]);
  });
  it("skips anything not pending", () => {
    const index = idx({ "/a.png": { state: "done", vector: [1], model: "m" }, "/b.png": pending, "/c.png": { state: "indexing" } });
    expect(planBatches(["/a.png", "/b.png", "/c.png"], index)).toEqual([["/b.png"]]);
  });
});

describe("blamedPath finds the file an error names", () => {
  it("returns the named path, longest match first, or null", () => {
    expect(blamedPath("/x/clip.mp4 is not a readable video (bad)", ["/x/a.png", "/x/clip.mp4"])).toBe("/x/clip.mp4");
    expect(blamedPath("model is loading", ["/x/a.png"])).toBeNull();
  });
});

describe("indexProgress counts the three states", () => {
  it("is busy while anything is pending or indexing, and never afterwards", () => {
    const index = idx({ "/a": { state: "done", vector: [1], model: "m" }, "/b": { state: "failed", error: "x" }, "/c": pending });
    expect(indexProgress(["/a", "/b", "/c"], index)).toEqual({ total: 3, done: 1, failed: 1, busy: true });
    expect(indexProgress(["/a", "/b"], index)).toEqual({ total: 2, done: 1, failed: 1, busy: false });
    expect(indexProgress(["/unknown"], index).busy).toBe(true);
  });
});

describe("scoreAgainst is a dot product over the indexed rows only", () => {
  it("ranks best first and leaves out failed or unindexed files", () => {
    const index = idx({
      "/near": { state: "done", vector: [1, 0], model: "m" },
      "/far": { state: "done", vector: [0, 1], model: "m" },
      "/broken": { state: "failed", error: "x" },
      "/late": pending,
    });
    expect(scoreAgainst([1, 0], ["/far", "/near", "/broken", "/late"], index)).toEqual([
      { path: "/near", score: 1 },
      { path: "/far", score: 0 },
    ]);
  });
});

describe("plainError", () => {
  it("drops a leading Python exception class and leaves other text alone", () => {
    expect(plainError("ValueError: no such file: /x.wav")).toBe("no such file: /x.wav");
    expect(plainError("model unloaded")).toBe("model unloaded");
  });
});
