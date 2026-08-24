// The share card's PURE parts — the caption, the provenance line, the
// filename, the layout arithmetic. The drawing itself is not tested here: it
// needs a real `CanvasRenderingContext2D` (bun has none), and a fake one would
// only assert that this file calls the methods this file calls. What IS worth
// pinning is the text a card carries away from the machine that made it, since
// that is the part somebody else has to read.
import { describe, expect, it } from "bun:test";
import type { AiBenchmarkMachine } from "@platform/lib/api";
import { availableMetrics } from "@apps/ai_models/lib/benchmark";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import {
  hardwareLine,
  metricSubtitle,
  provenanceLine,
  shareCardFilename,
  shareCardHeight,
  titleSuffix,
} from "./shareCard";

const MAC: AiBenchmarkMachine = {
  platform: "Darwin",
  arch: "arm64",
  cpuCount: 12,
  totalMemoryBytes: 32 * 1024 ** 3,
};

const metric = (capability: string, key: string) => {
  const spec = availableMetrics(capability, []).find((m) => m.key === key);
  if (!spec) throw new Error(`no ${key} metric for ${capability}`);
  return spec;
};

describe("hardwareLine", () => {
  it("names the machine the way a person would, not the way the kernel does", () => {
    expect(hardwareLine(MAC, "mps")).toBe("macOS · arm64 · 12 cores · 32 GB RAM · mps");
  });

  it("omits what the server did not report rather than printing a gap", () => {
    // Windows reports no RAM at all (`benchmark._total_memory_bytes`), and a
    // card saying "— RAM" reads as a bug in the card.
    expect(hardwareLine({ platform: "Windows", arch: "AMD64", cpuCount: null, totalMemoryBytes: null }, null))
      .toBe("Windows · AMD64");
  });

  it("keeps the device even when the machine block has not arrived", () => {
    // The device is the one part that changes the numbers by an order of
    // magnitude (`cpu` vs `mps`), so it must survive on its own.
    expect(hardwareLine(null, "cuda")).toBe("cuda");
    expect(hardwareLine(null, null)).toBe("");
  });
});

describe("provenanceLine", () => {
  it("names the version the runs were measured under", () => {
    expect(provenanceLine("0.7.3", new Date("2026-08-24T10:00:00Z"))).toContain("fused render 0.7.3");
  });

  it("still brands the card when no run reported a version", () => {
    const line = provenanceLine(null, new Date("2026-08-24T10:00:00Z"));
    expect(line.startsWith("fused render · ")).toBe(true);
  });
});

describe("metricSubtitle", () => {
  it("states the direction BOTH ways — the card has no page around it", () => {
    expect(metricSubtitle(metric("text-generation", "tokensPerSecond"))).toContain("higher is better");
    expect(metricSubtitle(metric("text-generation", "ttftMs"))).toContain("lower is better");
  });
});

describe("shareCardFilename", () => {
  it("slugs both identifying halves, so a folder of cards is readable", () => {
    expect(shareCardFilename("text-generation", metric("text-generation", "tokensPerSecond"))).toBe(
      "fused-render-benchmark-text-generation-tokens-per-second.png",
    );
  });
});

describe("shareCardHeight", () => {
  it("grows by one row's pitch per bar", () => {
    const one = shareCardHeight(1);
    const two = shareCardHeight(2);
    expect(two - one).toBe(28); // ROW_H + ROW_GAP
    expect(shareCardHeight(6) - shareCardHeight(1)).toBe(5 * 28);
  });

  it("leaves room for the chrome even with a single bar", () => {
    // Title, subtitle, axis and footer are all unconditional — a one-bar
    // card that came out row-height tall would have cropped them. There is
    // no separate brand row any more (the caption folded onto the title
    // line, the mark moved to the footer), so the floor here is lower than
    // it used to be — this pins the exact arithmetic rather than a loose
    // bound, so a stale term left behind by a future edit would fail here.
    expect(shareCardHeight(1)).toBe(198);
  });
});

describe("titleSuffix", () => {
  it("reads as one natural phrase after any capability label", () => {
    // The user's own wording: "speech to text local ai benchmark" — sentence
    // case, appended right after the label with no separator of its own
    // (the drawing code supplies the single space between them).
    const phrase = (capability: string) => `${capabilityLabel(capability)} ${titleSuffix()}`;
    expect(phrase("automatic-speech-recognition")).toBe("Speech to text local AI benchmark");
    expect(phrase("text-generation")).toBe("Text generation local AI benchmark");
    expect(phrase("text-to-image")).toBe("Image generation local AI benchmark");
  });
});
