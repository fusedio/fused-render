import { describe, expect, test } from "bun:test";
import { healWanted } from "./sync-state";

describe("healWanted", () => {
  test("the first sight of a watcher's value is a baseline", () => {
    expect(healWanted(null, "w1", 2)).toEqual({ heal: false, mark: { epoch: "w1", seq: 2 } });
  });

  test("a move on the same watcher asks for a full state, once", () => {
    const first = healWanted(null, "w1", 0).mark;
    const moved = healWanted(first, "w1", 1);
    expect(moved.heal).toBe(true);
    expect(healWanted(moved.mark, "w1", 1).heal).toBe(false);
  });

  test("a new watcher counts from 0 again: a baseline, not a move", () => {
    const old = { epoch: "w1", seq: 3 };
    expect(healWanted(old, "w2", 1)).toEqual({ heal: false, mark: { epoch: "w2", seq: 1 } });
  });

  test("a server without the field changes nothing", () => {
    const old = { epoch: "w1", seq: 3 };
    expect(healWanted(old, "w1", undefined)).toEqual({ heal: false, mark: old });
  });
});
