// The git decoration's two halves must agree: a row is never tinted without
// its badge, or badged without its tint. They are separate functions because
// they are called from separate places (the <tr> class and a cell's child), and
// this is where "separate" is kept from becoming "divergent".
import { describe, expect, it } from "bun:test";

import { GIT_MARKS, gitMarkFor, gitRowClass } from "./git-mark";

const STATES = Object.keys(GIT_MARKS);

describe("gitRowClass", () => {
  it("names one class per known state", () => {
    expect(STATES.map(gitRowClass)).toEqual([
      " git-conflicted",
      " git-modified",
      " git-untracked",
      " git-staged",
    ]);
  });

  it("is empty for a clean entry", () => {
    expect(gitRowClass(undefined)).toBe("");
  });

  it("is empty for a state this build does not know", () => {
    // A newer server can send a fifth state; an unstyled `git-…` class would
    // leave the row looking decorated and coloured like nothing.
    expect(gitRowClass("deleted")).toBe("");
  });

  it("does not resolve an inherited Object.prototype property", () => {
    // `GIT_MARKS[status] ?? null` resolves the PROTOTYPE CHAIN, not just the
    // object's own keys — `GIT_MARKS["constructor"]` is `Object`, a truthy
    // value, so this class name must not fall out of a cross-version guard
    // that only means to reject values this build has never heard of.
    expect(gitRowClass("constructor")).toBe("");
    expect(gitRowClass("toString")).toBe("");
    expect(gitRowClass("hasOwnProperty")).toBe("");
  });
});

describe("gitMarkFor", () => {
  it("gives every state a distinct letter", () => {
    const letters = STATES.map((s) => gitMarkFor(s)!.letter);
    expect(new Set(letters).size).toBe(STATES.length);
  });

  it("never lends git's staged-add letter to untracked", () => {
    // `A` means "staged add" in `git status`; reusing it here would make the
    // listing and the terminal disagree about what A is.
    expect(STATES.map((s) => gitMarkFor(s)!.letter)).not.toContain("A");
  });

  it("labels every state in words, for the tooltip and the screen reader", () => {
    for (const state of STATES) {
      expect(gitMarkFor(state)!.label.length).toBeGreaterThan(0);
    }
  });

  it("is null for a clean entry and for an unknown state", () => {
    expect(gitMarkFor(undefined)).toBeNull();
    expect(gitMarkFor("")).toBeNull();
    expect(gitMarkFor("deleted")).toBeNull();
  });

  it("is null for an inherited Object.prototype property", () => {
    // Same trap as gitRowClass above, on the other function that indexes
    // GIT_MARKS: `gitMarkFor("constructor")` must not return `Object`.
    expect(gitMarkFor("constructor")).toBeNull();
    expect(gitMarkFor("toString")).toBeNull();
  });

  it("agrees with gitRowClass on every input", () => {
    for (const input of [...STATES, "deleted", "", undefined]) {
      expect(!!gitMarkFor(input)).toBe(gitRowClass(input) !== "");
    }
  });
});
