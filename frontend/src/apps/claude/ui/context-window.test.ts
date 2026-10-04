// THE ARITHMETIC, AGAINST THE CLI'S OWN NUMBERS.
//
// Every expectation here is a value read out of Claude Code 2.1.278 and written
// down in `.claude-design/context-meter/claude-code-parity.md` — 967,000 and
// 167,000 above all, which are the two thresholds the whole feature hangs off.
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
      // Sonnet 4.6 has an enforced auto-compact window but an ordinary head.
      "claude-sonnet-4-6",
    ]) {
      expect(modelWindow(id), id).toBe(WINDOW_DEFAULT);
    }
  });

  it("understands the four bare aliases the composer's pill offers", () => {
    expect(modelWindow("fable")).toBe(WINDOW_1M);
    expect(modelWindow("opus")).toBe(WINDOW_1M);
    expect(modelWindow("sonnet")).toBe(WINDOW_1M);
    // Haiku is the one alias that is not a million-token model.
    expect(modelWindow("haiku")).toBe(WINDOW_DEFAULT);
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

describe("autoCompactWindow", () => {
  it("pins the `cIr` models to 200k however big their head is", () => {
    for (const id of [
      "claude-sonnet-4-6",
      "claude-opus-4-6",
      "claude-opus-4-8",
      "claude-opus-5",
    ]) {
      const win = autoCompactWindow(id);
      expect(win.window, id).toBe(200_000);
      expect(win.source, id).toBe("model-default");
      expect(win.enforced, id).toBe(true);
    }
  });

  it("leaves everybody else on their own window, unenforced", () => {
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
  it("lands on 967,000 for a 1M model — the docs' own number", () => {
    const t = contextThresholds("claude-sonnet-5");
    expect(t.model).toBe(WINDOW_1M);
    expect(t.effective).toBe(980_000);
    expect(t.compactAt).toBe(967_000);
    expect(t.warnAt).toBe(947_000);
    expect(t.enforced).toBe(false);
  });

  it("lands on 167,000 for a 200k model, warning at 147,000", () => {
    const t = contextThresholds("claude-haiku-4-5");
    expect(t.effective).toBe(180_000);
    expect(t.compactAt).toBe(167_000);
    expect(t.warnAt).toBe(147_000);
  });

  it("gives an enforced model BOTH numbers, and they disagree on purpose", () => {
    // Opus 5 has a million-token head and is compacted at 167k, so the pill's
    // percentage (over the head) and the warning line (over the compact
    // window) are measuring different things — spec §8 says so explicitly.
    const t = contextThresholds("claude-opus-5");
    expect(t.model).toBe(WINDOW_1M);
    expect(t.window).toBe(200_000);
    expect(t.compactAt).toBe(167_000);
    expect(t.enforced).toBe(true);
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
    // Sonnet 5 compacts at 967k; Haiku at 167k. 100% is the compaction.
    expect(usedPct("claude-sonnet-5", input(285_229))).toBe(29);
    expect(usedPct("claude-haiku-4-5", input(84_000))).toBe(50);
    expect(usedPct("claude-haiku-4-5", input(167_000))).toBe(100);
    expect(usedPct("claude-haiku-4-5", input(400_000))).toBe(100);
    expect(usedPct("claude-sonnet-5", null)).toBe(0);
  });

  it("counts the output in, and reads 100% exactly where contextLevel says compact", () => {
    // 180k in a million-token Opus 5 is 18% of the model's head — and past the
    // point where the CLI compacts it. The ring says the second, because that
    // is the question "can I keep going?" is asking (Akshil, 2026-09-21: one
    // number, not "77" in the ring under "87% context used").
    expect(usedPct("claude-opus-5", input(180_000))).toBe(100);
    expect(contextLevel("claude-opus-5", 180_000)).toBe("compact");
    const m = "claude-haiku-4-5";
    expect(usedPct(m, usage({ cache_read_input_tokens: 160_000, model: m }))).toBe(96);
    expect(
      usedPct(m, usage({ cache_read_input_tokens: 160_000, output_tokens: 7_000, model: m })),
    ).toBe(100);
  });
});

describe("contextLevel", () => {
  it("steps exactly at the CLI's thresholds, for a 200k model", () => {
    const m = "claude-haiku-4-5";
    expect(contextLevel(m, 146_999)).toBe("ok");
    expect(contextLevel(m, 147_000)).toBe("warn");
    expect(contextLevel(m, 166_999)).toBe("warn");
    expect(contextLevel(m, 167_000)).toBe("compact");
  });

  it("...and at 947k / 967k for a million-token one", () => {
    const m = "claude-sonnet-5";
    expect(contextLevel(m, 946_999)).toBe("ok");
    expect(contextLevel(m, 947_000)).toBe("warn");
    expect(contextLevel(m, 967_000)).toBe("compact");
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

  it("counts DOWN to auto-compact when a window is enforced", () => {
    // Opus 5: enforced 200k window, compact at 167k. 100 − round(150/167).
    expect(warnLine("claude-opus-5", input(150_000))).toBe(
      "10% until auto-compact",
    );
    // The SAME number the ring draws, in words.
    expect(usedPct("claude-opus-5", input(150_000))).toBe(90);
    expect(warnLine("claude-opus-5", input(167_000))).toBe(
      "0% until auto-compact",
    );
    // Past the threshold it does not go negative — `max(0, …)`.
    expect(warnLine("claude-opus-5", input(500_000))).toBe(
      "0% until auto-compact",
    );
  });

  it("counts UP through the effective window when none is enforced", () => {
    // Sonnet 5: compacts at 967k, warning from 947k. round(950/967).
    expect(warnLine("claude-sonnet-5", input(950_000))).toBe("98% context used");
    expect(usedPct("claude-sonnet-5", input(950_000))).toBe(98);
    expect(warnLine("claude-sonnet-5", input(980_000))).toBe("100% context used");
  });

  it("counts the output in, which is what pushes a turn over the line", () => {
    const m = "claude-haiku-4-5";
    expect(warnLine(m, usage({ cache_read_input_tokens: 146_000, model: m }))).toBe(
      "",
    );
    expect(
      warnLine(
        m,
        usage({
          cache_read_input_tokens: 146_000,
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
    ).toBe("Context: 285k of 967k tokens before auto-compact (29%)");
    expect(contextHint("claude-haiku-4-5", input(84_000))).toBe(
      "Context: 84k of 167k tokens before auto-compact (50%)",
    );
  });

  it("labels the post-compaction reading as the estimate it is", () => {
    expect(
      contextHint(
        "claude-sonnet-5",
        usage({ input_tokens: 9_876, model: "claude-sonnet-5", compacted: true }),
      ),
    ).toBe("Context: 9.8k of 967k tokens before auto-compact (1%) · compacted, estimate");
  });
});

describe("contextReport", () => {
  it("draws 100 squares on a 200k window, 200 on a million", () => {
    const small = contextReport("claude-haiku-4-5", input(84_000));
    expect(small.columns).toBe(10);
    expect(small.rows).toBe(10);
    expect(small.squares).toHaveLength(100);
    // 42% of 100 squares, and the rest free — no buffer on an unenforced
    // window, which is the CLI's own rule.
    expect(small.squares.filter((s) => s.kind === "messages")).toHaveLength(42);
    expect(small.squares.filter((s) => s.kind === "buffer")).toHaveLength(0);
    expect(small.squares.filter((s) => s.kind === "free")).toHaveLength(58);

    const big = contextReport("claude-sonnet-5", input(285_229));
    expect(big.columns).toBe(20);
    expect(big.squares).toHaveLength(200);
    expect(big.squares.filter((s) => s.kind === "messages")).toHaveLength(57);
  });

  it("holds the autocompact buffer back when a window is enforced", () => {
    // Opus 5: a 1M grid, and a buffer of 1,000,000 − 167,000 = 833,000.
    const r = contextReport("claude-opus-5", input(100_000, "claude-opus-5"));
    expect(r.squares).toHaveLength(200);
    expect(r.squares.filter((s) => s.kind === "buffer")).toHaveLength(167);
    expect(r.legend.map((row) => row.label)).toEqual([
      "Messages",
      "Autocompact buffer",
      "Free space",
    ]);
    expect(r.legend[1]!.tokens).toBe(833_000);
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
    // 80% of the 167k compaction point, which is the ring's own scale.
    expect(contextReport("claude-haiku-4-5", input(130_000)).suggestion).toBe("");
    expect(contextReport("claude-haiku-4-5", input(135_000)).suggestion).toBe(
      "Context is 81% full",
    );
    // …and the headline pair the popover prints is total / compactAt.
    const r = contextReport("claude-haiku-4-5", input(135_000));
    expect([r.total, r.compactAt, r.pct]).toEqual([135_000, 167_000, 81]);
  });

  it("gives a used segment at least one square, and never more than the grid", () => {
    const tiny = contextReport("claude-haiku-4-5", input(200));
    expect(tiny.squares.filter((s) => s.kind === "messages")).toHaveLength(1);
    const over = contextReport("claude-haiku-4-5", input(900_000));
    expect(over.squares).toHaveLength(100);
    expect(over.squares.filter((s) => s.kind === "free")).toHaveLength(0);
  });
});
