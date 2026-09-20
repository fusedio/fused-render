import { describe, expect, it } from "bun:test";
import {
  WINDOW_1M,
  WINDOW_DEFAULT,
  contextFill,
  contextLevel,
  contextPercent,
  contextSentence,
  contextWindowFor,
  contextWindowOf,
  formatTokens,
} from "./context-window";

describe("contextWindowFor", () => {
  it("reads the CLI's [1m] qualifier as a million-token window", () => {
    expect(contextWindowFor("claude-opus-5[1m]")).toBe(WINDOW_1M);
    expect(contextWindowFor("fable[1m]")).toBe(WINDOW_1M);
    // Case and stray space are the CLI's business, not ours to be strict about.
    expect(contextWindowFor("sonnet[1M]")).toBe(WINDOW_1M);
    expect(contextWindowFor(" opus[1m] ")).toBe(WINDOW_1M);
  });

  it("gives everything else the 200k default, unknown ids included", () => {
    expect(contextWindowFor("sonnet")).toBe(WINDOW_DEFAULT);
    expect(contextWindowFor("claude-sonnet-4-5")).toBe(WINDOW_DEFAULT);
    // A qualifier that is not the context one does not buy the big window.
    expect(contextWindowFor("opus[thinking]")).toBe(WINDOW_DEFAULT);
    // ...and nor does "1m" loose in the middle of an id.
    expect(contextWindowFor("claude-1m-preview")).toBe(WINDOW_DEFAULT);
  });

  it("answers the default rather than throwing on nothing at all", () => {
    // A brand-new chat's transcript model is "", and the pill can be empty
    // while the defaults read is still out. Guessing LARGE would draw room
    // that is not there, so the small window is the safe fallback.
    expect(contextWindowFor("")).toBe(WINDOW_DEFAULT);
    expect(contextWindowFor(null)).toBe(WINDOW_DEFAULT);
    expect(contextWindowFor(undefined)).toBe(WINDOW_DEFAULT);
  });
});

describe("contextWindowOf", () => {
  it("keeps the id's window while the count fits inside it", () => {
    expect(contextWindowOf(84_000, "sonnet")).toBe(WINDOW_DEFAULT);
    expect(contextWindowOf(200_000, "sonnet")).toBe(WINDOW_DEFAULT);
    expect(contextWindowOf(84_000, "opus[1m]")).toBe(WINDOW_1M);
  });

  it("escalates on PROOF: 285k tokens never fitted in a 200k window", () => {
    // Measured against a live 1M session on 2026-09-20: the transcript reports
    // 285,229 tokens under the bare id "claude-fable-5-1", because the `[1m]`
    // qualifier is a CLI spelling the wire never carries back. Without this the
    // meter pinned at a red 100% for every million-token chat.
    expect(contextWindowOf(285_229, "claude-fable-5-1")).toBe(WINDOW_1M);
  });

  it("only ever goes up, and never past the biggest window there is", () => {
    // A count over the million is a full meter, not a two-million window: there
    // is nothing above 1M to escalate to, and inventing one would draw headroom
    // nothing has demonstrated.
    expect(contextWindowOf(1_400_000, "opus[1m]")).toBe(WINDOW_1M);
    expect(contextWindowOf(NaN, "sonnet")).toBe(WINDOW_DEFAULT);
  });
});

describe("formatTokens", () => {
  it("prints small counts whole and larger ones with a unit", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(950)).toBe("950");
    expect(formatTokens(999)).toBe("999");
    expect(formatTokens(1000)).toBe("1k");
    expect(formatTokens(1250)).toBe("1.2k");
    expect(formatTokens(84_000)).toBe("84k");
    expect(formatTokens(199_999)).toBe("199k");
    expect(formatTokens(200_000)).toBe("200k");
    expect(formatTokens(1_200_000)).toBe("1.2M");
    expect(formatTokens(1_000_000)).toBe("1M");
  });

  it("rounds DOWN, so it never claims tokens that were not spent", () => {
    expect(formatTokens(1999)).toBe("1.9k");
    expect(formatTokens(999_999)).toBe("999k");
  });

  it("says 0 for a number that is not one", () => {
    expect(formatTokens(-5)).toBe("0");
    expect(formatTokens(NaN)).toBe("0");
    expect(formatTokens(Infinity)).toBe("0");
  });
});

describe("contextFill", () => {
  it("is the ratio, clamped at both ends", () => {
    expect(contextFill(0, 200_000)).toBe(0);
    expect(contextFill(100_000, 200_000)).toBe(0.5);
    expect(contextFill(400_000, 200_000)).toBe(1);
    expect(contextFill(-10, 200_000)).toBe(0);
  });

  it("reads a missing window as empty, never as full", () => {
    expect(contextFill(50_000, 0)).toBe(0);
    expect(contextFill(50_000, NaN)).toBe(0);
    expect(contextFill(NaN, 200_000)).toBe(0);
  });
});

describe("contextLevel", () => {
  it("stays quiet until 70%, warns to 90%, then reads as full", () => {
    expect(contextLevel(0)).toBe("ok");
    expect(contextLevel(0.699)).toBe("ok");
    expect(contextLevel(0.7)).toBe("warn");
    expect(contextLevel(0.899)).toBe("warn");
    expect(contextLevel(0.9)).toBe("full");
    expect(contextLevel(1)).toBe("full");
  });
});

describe("contextPercent", () => {
  it("floors, so 100% means there is genuinely no room left", () => {
    expect(contextPercent(0)).toBe(0);
    expect(contextPercent(0.4211)).toBe(42);
    expect(contextPercent(0.999)).toBe(99);
    expect(contextPercent(1)).toBe(100);
  });
});

describe("contextSentence", () => {
  it("is the same sentence the tooltip and the label both say", () => {
    expect(contextSentence(84_000, 200_000)).toBe("Context 84k of 200k · 116k left");
    expect(contextSentence(1_200_000, 1_000_000)).toBe("Context 1.2M of 1M · 0 left");
  });
});
