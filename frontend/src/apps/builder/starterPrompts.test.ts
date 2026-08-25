// The composer's starter pool is a CONTENT invariant, not logic: the chip row
// filters down to one capability the moment a Playground model is attached, so
// every capability needs enough briefs of its own to fill that row and still
// have something left for the shuffle button to advance to. Four is the row
// width; anything below it renders a short row, and zero renders none at all.
// These tests are the only thing standing between that and a pool someone
// extends by adding five more text-generation ideas.
import { describe, expect, it } from "bun:test";
import {
  STARTER_CAPABILITIES,
  STARTER_PROMPTS,
  shuffleStarters,
  startersFor,
} from "./starterPrompts";

// The chip row's width, and therefore the floor for every bucket.
const ROW = 4;

describe("STARTER_PROMPTS", () => {
  it("has at least a full row of starters for every capability", () => {
    for (const cap of STARTER_CAPABILITIES) {
      const n = STARTER_PROMPTS.filter((s) => s.capability === cap).length;
      expect(n).toBeGreaterThanOrEqual(ROW);
    }
  });

  it("has at least a full row of starters that need no AI at all", () => {
    expect(STARTER_PROMPTS.filter((s) => s.capability === null).length).toBeGreaterThanOrEqual(ROW);
  });

  it("uses only the capabilities the composer can filter by", () => {
    // A typo ("text-to-images") would otherwise be a silent sixth bucket: no
    // annotation ever matches it, so its briefs would only ever surface in the
    // unfiltered row and the capability they were written for would be short.
    const seen = new Set(STARTER_PROMPTS.map((s) => s.capability).filter((c) => c !== null));
    expect([...seen].sort()).toEqual([...STARTER_CAPABILITIES].sort());
  });

  it("names every starter once — the label is the chip's React key", () => {
    const labels = STARTER_PROMPTS.map((s) => s.label);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("gives every starter a brief long enough to build from", () => {
    // The chip drops this straight into the composer; a one-liner is the thing
    // this pool exists to avoid.
    for (const s of STARTER_PROMPTS) {
      expect(s.prompt.length).toBeGreaterThan(120);
      expect(s.glyph).toBeTruthy();
    }
  });

  it("tells the session to use the local models in every AI brief", () => {
    for (const s of STARTER_PROMPTS.filter((x) => x.capability !== null)) {
      expect(s.prompt).toContain("fused.ai");
    }
  });

  it("keeps the no-AI briefs free of AI instructions", () => {
    for (const s of STARTER_PROMPTS.filter((x) => x.capability === null)) {
      expect(s.prompt).not.toContain("fused.ai");
    }
  });
});

describe("shuffleStarters", () => {
  it("returns a permutation and leaves the pool it was given alone", () => {
    const before = STARTER_PROMPTS.map((s) => s.label);
    // rand() = 0 sends every element to index 0 — a full reversal-ish draw, and
    // the cheapest way to prove the copy is what moved.
    const out = shuffleStarters(STARTER_PROMPTS, () => 0);
    expect(STARTER_PROMPTS.map((s) => s.label)).toEqual(before);
    expect(out.map((s) => s.label).sort()).toEqual([...before].sort());
    expect(out.map((s) => s.label)).not.toEqual(before);
  });

  it("mixes the capabilities, which is the whole point of shuffling", () => {
    // The pool is authored grouped by capability, so the first row of the
    // unshuffled array is all one kind. A real draw should not be.
    const kinds = new Set(shuffleStarters(STARTER_PROMPTS).slice(0, 8).map((s) => s.capability));
    expect(kinds.size).toBeGreaterThan(1);
  });
});

describe("startersFor", () => {
  it("offers the whole mixed pool when no model is attached", () => {
    expect(startersFor(null).length).toBe(STARTER_PROMPTS.length);
    expect(startersFor(undefined).length).toBe(STARTER_PROMPTS.length);
  });

  it("narrows to one capability's briefs, and only that capability's", () => {
    for (const cap of STARTER_CAPABILITIES) {
      const hits = startersFor(cap);
      expect(hits.length).toBeGreaterThanOrEqual(ROW);
      expect(hits.every((s) => s.capability === cap)).toBe(true);
    }
  });

  it("falls back to the whole pool for a capability it has no briefs for", () => {
    // An older `?annot=` with no capability, or a capability added to the
    // Playground before starters were written for it: a row of chips beats an
    // empty row.
    expect(startersFor("depth-estimation").length).toBe(STARTER_PROMPTS.length);
  });
});
