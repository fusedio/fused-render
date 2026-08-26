import { describe, expect, it } from "bun:test";
import { formatSize } from "@platform/lib/format";
import { fitNote } from "./fitNote";

// SPEC AI-16c: `fit` widened from a bare verdict string to
// `{verdict, basis, footprintBytes}`, and the badge's copy now splits on
// BOTH — a measured verdict is worded as a fact (figure included), a
// declared/download one keeps the original hedge (no figure).

describe("fitNote", () => {
  it("is null when nothing is known", () => {
    expect(fitNote(null)).toBeNull();
    expect(fitNote(undefined)).toBeNull();
  });

  describe("basis: download", () => {
    it("words easy/tight/no as a hedge, with no figure", () => {
      expect(fitNote({ verdict: "easy", basis: "download", footprintBytes: 4e9 })?.text).toBe(
        "Runs comfortably here",
      );
      expect(fitNote({ verdict: "tight", basis: "download", footprintBytes: 4e9 })?.text).toBe(
        "Tight fit on this machine",
      );
      expect(fitNote({ verdict: "no", basis: "download", footprintBytes: 4e9 })?.text).toBe(
        "Likely too big for this machine",
      );
    });

    it("titles the badge as a judgement, not a measurement", () => {
      expect(fitNote({ verdict: "easy", basis: "download", footprintBytes: 4e9 })?.title).toBe(
        "Judged against this machine's memory",
      );
    });
  });

  describe("basis: declared", () => {
    it("words easy/tight/no the same hedged way as download", () => {
      expect(fitNote({ verdict: "tight", basis: "declared", footprintBytes: 4e9 })?.text).toBe(
        "Tight fit on this machine",
      );
      expect(fitNote({ verdict: "tight", basis: "declared", footprintBytes: 4e9 })?.title).toBe(
        "Judged against this machine's memory",
      );
    });
  });

  describe("basis: measured", () => {
    const bytes = 28 * 1024 * 1024 * 1024; // 28 GB, base-1024 — same units formatSize uses everywhere else

    it("words the verdict as a FACT, with the figure included", () => {
      expect(fitNote({ verdict: "easy", basis: "measured", footprintBytes: bytes })?.text).toBe(
        `Ran comfortably here (${formatSize(bytes)})`,
      );
      expect(fitNote({ verdict: "tight", basis: "measured", footprintBytes: bytes })?.text).toBe(
        `Ran here, tight (${formatSize(bytes)})`,
      );
      expect(fitNote({ verdict: "no", basis: "measured", footprintBytes: bytes })?.text).toBe(
        `Ran here, over budget (${formatSize(bytes)})`,
      );
    });

    it("a measured 'no' is reachable and worded as a fact, not a contradiction", () => {
      // AI-16b/AI-16c: the footprint store only ever holds models that ran —
      // a measured "no" describes a run that happened while nothing else was
      // competing for the reserve-adjusted budget, not an impossible state.
      const note = fitNote({ verdict: "no", basis: "measured", footprintBytes: bytes });
      expect(note?.text).toContain("Ran here");
      expect(note?.dot).toBe("bg-[var(--error)]");
    });

    it("titles the badge as a measurement, not a guess", () => {
      expect(fitNote({ verdict: "easy", basis: "measured", footprintBytes: bytes })?.title).toBe(
        "Measured on this machine",
      );
    });
  });

  describe("the dot hue", () => {
    it("tints only the two verdicts that ask the reader to do something differently", () => {
      expect(fitNote({ verdict: "easy", basis: "download", footprintBytes: 1 })?.dot).toBe(
        "bg-emerald-500",
      );
      expect(fitNote({ verdict: "tight", basis: "download", footprintBytes: 1 })?.dot).toBe(
        "bg-[var(--warning)]",
      );
      expect(fitNote({ verdict: "no", basis: "download", footprintBytes: 1 })?.dot).toBe(
        "bg-[var(--error)]",
      );
    });

    it("is the same hue regardless of basis — only verdict decides the colour", () => {
      expect(fitNote({ verdict: "tight", basis: "measured", footprintBytes: 1 })?.dot).toBe(
        fitNote({ verdict: "tight", basis: "download", footprintBytes: 1 })?.dot,
      );
    });
  });
});
