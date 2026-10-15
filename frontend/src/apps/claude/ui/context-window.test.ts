// THE ARITHMETIC, AGAINST THE CLI'S OWN NUMBERS.
//
// Every expectation here is a value measured against Claude Code 2.1.300
// (`modelUsage.<id>.contextWindow` and `/context`) — 997,000 and 197,000 above
// all, which are the two compaction points the whole feature hangs off.
// The point of the file is that a refactor cannot quietly move this meter ten
// points away from the meter the same reader has in their terminal.
import { describe, expect, it } from "bun:test";
import type { ContextUsage } from "../protocol/types";
import {
  WINDOW_1M,
  WINDOW_DEFAULT,
  autoCompactWindow,
  contextHint,
  contextInput,
  contextLevel,
  contextReport,
  contextThresholds,
  contextTotal,
  formatTokens,
  groupDigits,
  modelWindow,
  usedPct,
  warnLine,
} from "./context-window";

function usage(over: Partial<ContextUsage> = {}): ContextUsage {
  return {
    input_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    output_tokens: 0,
    model: "",
    compacted: false,
    ...over,
  };
}

/** A reading of exactly `n` input tokens, however it is spelled on the wire. */
const input = (n: number, model = "claude-sonnet-5"): ContextUsage =>
  usage({ cache_read_input_tokens: n, model });

describe("modelWindow", () => {
  it("reads the CLI's `[1m]` qualifier as a million, on an alias or a full id", () => {
    expect(modelWindow("claude-opus-5[1m]")).toBe(WINDOW_1M);
    expect(modelWindow("opus[1m]")).toBe(WINDOW_1M);
    expect(modelWindow("sonnet[1M]")).toBe(WINDOW_1M);
    // ...and it wins over the not-1M list, which is what the qualifier is FOR.
    expect(modelWindow("claude-haiku-4-5[1m]")).toBe(WINDOW_1M);
  });

  it("gives the natively-1M models their million", () => {
    for (const id of [
      "claude-fable-5-1",
      "claude-sonnet-5",
      "claude-opus-5",
      "claude-opus-4-7",
      "claude-opus-4-8",
      "claude-haiku-5",
    ]) {
      expect(modelWindow(id), id).toBe(WINDOW_1M);
    }
  });

  it("keeps the models the CLI lists as NOT 1M-capable at 200k", () => {
    for (const id of [
      "claude-haiku-4-5",
      "claude-3-5-sonnet-20241022",
      "claude-opus-4-0",
      "claude-opus-4-1",
      "claude-opus-4-5",
      // 2.1.300: Sonnet 4.6 reports a 200k window.
      "claude-sonnet-4-6",
      "claude-opus-4-6",
    ]) {
      expect(modelWindow(id), id).toBe(WINDOW_DEFAULT);
    }
  });

  it("understands the four bare aliases the composer's pill offers", () => {
    expect(modelWindow("fable")).toBe(WINDOW_1M);
    expect(modelWindow("opus")).toBe(WINDOW_1M);
    expect(modelWindow("sonnet")).toBe(WINDOW_1M);
    // Every 5.x model is a million, and the bare alias names the 5.x one.
    expect(modelWindow("haiku")).toBe(WINDOW_1M);
  });

  it("answers the SMALL default for nothing at all, and for an unknown id", () => {
    // Guessing large would draw room that is not there, which is the one
    // direction this number must never be wrong in.
    expect(modelWindow("")).toBe(WINDOW_DEFAULT);
    expect(modelWindow(null)).toBe(WINDOW_DEFAULT);
    expect(modelWindow(undefined)).toBe(WINDOW_DEFAULT);
    expect(modelWindow("some-model-nobody-has-heard-of")).toBe(WINDOW_DEFAULT);
  });
});

describe("the CLI's own window", () => {
  it("wins over the table when the reading carries one", () => {
    // agent.py stamps `modelUsage.<model>.contextWindow` off the result row.
    // A 200k-table model the CLI says is 1M reads as 1M everywhere — and the
    // other way round, so the table can be wrong in EITHER direction.
    const big = usage({ cache_read_input_tokens: 10, model: "claude-haiku-4-5", window: 1_000_000 });
    expect(modelWindow("claude-haiku-4-5", big)).toBe(WINDOW_1M);
    expect(contextThresholds("claude-haiku-4-5", big).compactAt).toBe(997_000);
    const small = usage({ cache_read_input_tokens: 10, model: "claude-fable-5-1", window: 200_000 });
    expect(modelWindow("claude-fable-5-1", small)).toBe(WINDOW_DEFAULT);
    expect(usedPct("claude-fable-5-1", usage({ cache_read_input_tokens: 98_500, model: "claude-fable-5-1", window: 200_000 }))).toBe(50);
    expect(contextHint("claude-fable-5-1", usage({ cache_read_input_tokens: 98_500, model: "claude-fable-5-1", window: 200_000 }))).toBe(
      "Context: 98k of 197k tokens before auto-compact (50%)",
    );
  });

  it("falls back to the table when the window is absent, null or junk", () => {
    expect(modelWindow("claude-fable-5-1", usage({ model: "claude-fable-5-1" }))).toBe(WINDOW_1M);
    expect(modelWindow("claude-fable-5-1", usage({ model: "claude-fable-5-1", window: null }))).toBe(WINDOW_1M);
    expect(modelWindow("claude-haiku-4-5", usage({ model: "claude-haiku-4-5", window: 0 }))).toBe(WINDOW_DEFAULT);
    expect(modelWindow("claude-haiku-4-5", usage({ model: "claude-haiku-4-5", window: Number.NaN }))).toBe(WINDOW_DEFAULT);
  });
});

describe("autoCompactWindow", () => {
  it("pins nobody to a 200k auto-compact window any more", () => {
    // 2.1.278 pinned these; 2.1.300 does not (`/context` on Opus 5 and 4.8
    // shows ~977k free of 1m).
    for (const id of ["claude-opus-4-8", "claude-opus-5", "claude-sonnet-4-6"]) {
      const win = autoCompactWindow(id);
      expect(win.window, id).toBe(modelWindow(id));
      expect(win.source, id).toBe("auto");
      expect(win.enforced, id).toBe(false);
    }
  });

  it("leaves every model on its own window, unenforced", () => {
    expect(autoCompactWindow("claude-sonnet-5")).toEqual({
      window: WINDOW_1M,
      source: "auto",
      enforced: false,
    });
    expect(autoCompactWindow("claude-haiku-4-5")).toEqual({
      window: WINDOW_DEFAULT,
      source: "auto",
      enforced: false,
    });
  });
});

describe("contextThresholds", () => {
  it("lands on 997,000 for a 1M model, warning at 977,000", () => {
    const t = contextThresholds("claude-sonnet-5");
    expect(t.model).toBe(WINDOW_1M);
    expect(t.effective).toBe(1_000_000);
    expect(t.compactAt).toBe(997_000);
    expect(t.warnAt).toBe(977_000);
    expect(t.enforced).toBe(false);
  });

  it("lands on 197,000 for a 200k model, warning at 177,000", () => {
    const t = contextThresholds("claude-haiku-4-5");
    expect(t.effective).toBe(200_000);
    expect(t.compactAt).toBe(197_000);
    expect(t.warnAt).toBe(177_000);
  });

  it("gives Opus 5 the same 1M numbers as any other million-token model", () => {
    // No model is pinned to a 200k compact window in 2.1.300, so the pill's
    // head and the compact window are one number.
    const t = contextThresholds("claude-opus-5");
    expect(t.model).toBe(WINDOW_1M);
    expect(t.window).toBe(WINDOW_1M);
    expect(t.compactAt).toBe(997_000);
    expect(t.enforced).toBe(false);
  });
});

describe("the two sums", () => {
  it("counts input three ways and leaves the output out of it", () => {
    const u = usage({
      input_tokens: 3,
      cache_creation_input_tokens: 20,
      cache_read_input_tokens: 2000,
      output_tokens: 9,
    });
    // The statusline's definition, verbatim in the docs: input_tokens +
    // cache_creation_input_tokens + cache_read_input_tokens.
    expect(contextInput(u)).toBe(2023);
    // ...and the auto-compact arithmetic, which adds what came back.
    expect(contextTotal(u)).toBe(2032);
    expect(contextInput(null)).toBe(0);
    expect(contextTotal(undefined)).toBe(0);
  });

  it("ignores a field that is not a number, rather than answering NaN", () => {
    const u = usage({ cache_read_input_tokens: 12 });
    (u as unknown as Record<string, unknown>).input_tokens = "lots";
    expect(contextInput(u)).toBe(12);
  });
});

describe("usedPct", () => {
  it("is the whole conversation over the auto-compact point, rounded and clamped", () => {
    // Sonnet 5 compacts at 997k; Haiku 4.5 at 197k. 100% is the compaction.
    expect(usedPct("claude-sonnet-5", input(285_229))).toBe(29);
    expect(usedPct("claude-haiku-4-5", input(84_000))).toBe(43);
    expect(usedPct("claude-haiku-4-5", input(197_000))).toBe(100);
    expect(usedPct("claude-haiku-4-5", input(400_000))).toBe(100);
    expect(usedPct("claude-sonnet-5", null)).toBe(0);
  });

  it("counts the output in, and reads 100% exactly where contextLevel says compact", () => {
    // 180k in a million-token Opus 5 is 18% — nowhere near compaction now that
    // nothing is pinned to a 200k window.
    expect(usedPct("claude-opus-5", input(180_000))).toBe(18);
    expect(contextLevel("claude-opus-5", 180_000)).toBe("ok");
    const m = "claude-haiku-4-5";
    expect(usedPct(m, usage({ cache_read_input_tokens: 190_000, model: m }))).toBe(96);
    expect(
      usedPct(m, usage({ cache_read_input_tokens: 190_000, output_tokens: 7_000, model: m })),
    ).toBe(100);
    expect(contextLevel(m, 197_000)).toBe("compact");
  });
});

describe("contextLevel", () => {
  it("steps exactly at the CLI's thresholds, for a 200k model", () => {
    const m = "claude-haiku-4-5";
    expect(contextLevel(m, 176_999)).toBe("ok");
    expect(contextLevel(m, 177_000)).toBe("warn");
    expect(contextLevel(m, 196_999)).toBe("warn");
    expect(contextLevel(m, 197_000)).toBe("compact");
  });

  it("...and at 977k / 997k for a million-token one", () => {
    const m = "claude-sonnet-5";
    expect(contextLevel(m, 976_999)).toBe("ok");
    expect(contextLevel(m, 977_000)).toBe("warn");
    expect(contextLevel(m, 997_000)).toBe("compact");
  });

  it("is quiet about a conversation that has not started", () => {
    expect(contextLevel("claude-sonnet-5", 0)).toBe("ok");
    expect(contextLevel("claude-sonnet-5", NaN)).toBe("ok");
  });
});

describe("warnLine", () => {
  it("says nothing at all below the warning threshold", () => {
    expect(warnLine("claude-sonnet-5", input(100_000))).toBe("");
    expect(warnLine("claude-sonnet-5", null)).toBe("");
  });

  it("counts UP to the compaction on every model, Opus 5 included", () => {
    // Sonnet 5: compacts at 997k, warning from 977k. round(980/997).
    expect(warnLine("claude-sonnet-5", input(980_000))).toBe("98% context used");
    expect(usedPct("claude-sonnet-5", input(980_000))).toBe(98);
    // Past the threshold it clamps at 100.
    expect(warnLine("claude-sonnet-5", input(1_100_000))).toBe("100% context used");
    // Nothing is enforced, so there is no "until auto-compact" spelling.
    expect(warnLine("claude-opus-5", input(980_000))).toBe("98% context used");
  });

  it("counts the output in, which is what pushes a turn over the line", () => {
    const m = "claude-haiku-4-5";
    expect(warnLine(m, usage({ cache_read_input_tokens: 176_000, model: m }))).toBe(
      "",
    );
    expect(
      warnLine(
        m,
        usage({
          cache_read_input_tokens: 176_000,
          output_tokens: 2_000,
          model: m,
        }),
      ),
    ).not.toBe("");
  });
});

describe("the printed numbers", () => {
  it("shortens a token count without ever rounding it up", () => {
    expect(formatTokens(285_229)).toBe("285k");
    expect(formatTokens(1_000_000)).toBe("1M");
    expect(formatTokens(1_250_000)).toBe("1.2M");
    expect(formatTokens(999)).toBe("999");
    expect(formatTokens(-5)).toBe("0");
    expect(formatTokens(NaN)).toBe("0");
  });

  it("groups thousands the one way, whatever locale the machine is in", () => {
    expect(groupDigits(285_229)).toBe("285,229");
    expect(groupDigits(1_000_000)).toBe("1,000,000");
    expect(groupDigits(42)).toBe("42");
  });
});

describe("contextHint", () => {
  it("is the whole sentence the pill says, in both of its voices", () => {
    expect(
      contextHint("claude-fable-5-1", input(285_229, "claude-fable-5-1")),
    ).toBe("Context: 285k of 997k tokens before auto-compact (29%)");
    expect(contextHint("claude-haiku-4-5", input(84_000))).toBe(
      "Context: 84k of 197k tokens before auto-compact (43%)",
    );
  });

  it("labels the post-compaction reading as the estimate it is", () => {
    expect(
      contextHint(
        "claude-sonnet-5",
        usage({ input_tokens: 9_876, model: "claude-sonnet-5", compacted: true }),
      ),
    ).toBe("Context: 9.8k of 997k tokens before auto-compact (1%) · compacted, estimate");
  });
});

describe("contextReport", () => {
  it("draws 100 squares on a 200k window, 200 on a million", () => {
    const small = contextReport("claude-haiku-4-5", input(84_000));
    expect(small.columns).toBe(10);
    expect(small.rows).toBe(10);
    expect(small.squares).toHaveLength(100);
    // 42% of 100 squares, and the rest free — no buffer row, which is what
    // 2.1.300's `/context` shows for every model.
    expect(small.squares.filter((s) => s.kind === "messages")).toHaveLength(42);
    expect(small.squares.filter((s) => s.kind === "buffer")).toHaveLength(0);
    expect(small.squares.filter((s) => s.kind === "free")).toHaveLength(58);

    const big = contextReport("claude-sonnet-5", input(285_229));
    expect(big.columns).toBe(20);
    expect(big.squares).toHaveLength(200);
    expect(big.squares.filter((s) => s.kind === "messages")).toHaveLength(57);
  });

  it("draws no autocompact buffer on any model", () => {
    // Opus 5: a 1M grid, 100k of it used, the rest free.
    const r = contextReport("claude-opus-5", input(100_000, "claude-opus-5"));
    expect(r.squares).toHaveLength(200);
    expect(r.squares.filter((s) => s.kind === "buffer")).toHaveLength(0);
    expect(r.squares.filter((s) => s.kind === "messages")).toHaveLength(20);
    expect(r.legend.map((row) => row.label)).toEqual(["Messages", "Free space"]);
    expect(r.legend[1]!.tokens).toBe(900_000);
  });

  it("names the one category we can honestly claim, and says so", () => {
    const r = contextReport("claude-haiku-4-5", input(84_000));
    expect(r.legend.map((row) => row.label)).toEqual(["Messages", "Free space"]);
    const messages = r.legend[0]!;
    expect(messages.tokens).toBe(84_000);
    expect(messages.pct).toBe("42.0");
    // We have no per-category split, so the row says the whole sum is in play
    // rather than implying the other ten categories measured zero.
    expect(messages.note).toBe("(all context in use)");
    expect(r.legend[1]!.tokens).toBe(116_000);
  });

  it("suggests nothing until 80%, then quotes the CLI's own line", () => {
    // 80% of the 197k compaction point, which is the ring's own scale.
    expect(contextReport("claude-haiku-4-5", input(150_000)).suggestion).toBe("");
    expect(contextReport("claude-haiku-4-5", input(160_000)).suggestion).toBe(
      "Context is 81% full",
    );
    // …and the headline pair the popover prints is total / compactAt.
    const r = contextReport("claude-haiku-4-5", input(160_000));
    expect([r.total, r.compactAt, r.pct]).toEqual([160_000, 197_000, 81]);
  });

  it("gives a used segment at least one square, and never more than the grid", () => {
    const tiny = contextReport("claude-haiku-4-5", input(200));
    expect(tiny.squares.filter((s) => s.kind === "messages")).toHaveLength(1);
    const over = contextReport("claude-haiku-4-5", input(900_000));
    expect(over.squares).toHaveLength(100);
    expect(over.squares.filter((s) => s.kind === "free")).toHaveLength(0);
  });
});
