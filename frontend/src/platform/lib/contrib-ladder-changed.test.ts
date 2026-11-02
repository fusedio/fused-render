// `notifyLadderChanged` / `onLadderChanged` (contrib.ts): the signal the
// Share sheet sends when it moves a folder's rung, so the app page behind it
// re-reads instead of staying on the old rung until a reload.
import { describe, expect, test } from "bun:test";

import { ladderListenerCount, notifyLadderChanged, onLadderChanged } from "./contrib";

describe("ladder changed signal", () => {
  test("delivers to every listener on that folder and no other", () => {
    const hits: string[] = [];
    const offA1 = onLadderChanged("/apps/a", () => hits.push("a1"));
    const offA2 = onLadderChanged("/apps/a/", () => hits.push("a2"));
    const offB = onLadderChanged("/apps/b", () => hits.push("b"));
    notifyLadderChanged("/apps/a");
    expect(hits.sort()).toEqual(["a1", "a2"]);
    offA1();
    offA2();
    offB();
  });

  test("trailing slashes do not split a folder", () => {
    let n = 0;
    const off = onLadderChanged("/apps/x", () => (n += 1));
    notifyLadderChanged("/apps/x/");
    notifyLadderChanged("/apps/x//");
    expect(n).toBe(2);
    off();
  });

  test("unsubscribe stops delivery and frees the folder's slot", () => {
    let n = 0;
    const off = onLadderChanged("/apps/y", () => (n += 1));
    expect(ladderListenerCount("/apps/y")).toBe(1);
    off();
    off(); // idempotent
    notifyLadderChanged("/apps/y");
    expect(n).toBe(0);
    expect(ladderListenerCount("/apps/y")).toBe(0);
  });

  test("a notify with nobody listening is a no-op", () => {
    expect(() => notifyLadderChanged("/apps/nobody")).not.toThrow();
  });

  test("a listener that unsubscribes mid-delivery does not break the others", () => {
    const hits: string[] = [];
    const off1 = onLadderChanged("/apps/z", () => {
      hits.push("1");
      off1();
    });
    const off2 = onLadderChanged("/apps/z", () => hits.push("2"));
    notifyLadderChanged("/apps/z");
    expect(hits).toEqual(["1", "2"]);
    off2();
  });
});
