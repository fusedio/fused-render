// The session-lifetime pane width. Testable at all only because it is a plain
// module variable and not storage — which is also the point of the module (see
// pane-store.ts): a refresh is the reset, so nothing may outlive the document.
import { beforeEach, describe, expect, test } from "bun:test";
import { getPaneFrac, setPaneFrac } from "./pane-store";

beforeEach(() => setPaneFrac(null));

describe("pane-store", () => {
  test("nothing chosen yet reads as null, not as a number", () => {
    // `null` is the state that lets the pane FOLLOW the container's width
    // (pane.ts). Seeding a default here would freeze it instead.
    expect(getPaneFrac()).toBeNull();
  });

  test("a dragged fraction is remembered", () => {
    setPaneFrac(0.42);
    expect(getPaneFrac()).toBe(0.42);
  });

  test("one width for every folder — the store is not keyed by path", () => {
    // THE BUG this module exists for: the width used to live in the per-path
    // viewstate map, so walking from a folder you had dragged into one you had
    // not snapped the divider between your width and the breakpoint default on
    // every navigation. There is now one width and every surface reads it.
    setPaneFrac(0.42);
    setPaneFrac(0.61);
    expect(getPaneFrac()).toBe(0.61);
  });

  test("the choice can be given back", () => {
    setPaneFrac(0.42);
    setPaneFrac(null);
    expect(getPaneFrac()).toBeNull();
  });
});
