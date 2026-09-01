import { describe, expect, it } from "bun:test";
import {
  ageLabel,
  familyDisplay,
  fitCell,
  popLabel,
  runModeLabel,
  speedLabel,
  variantLabel,
} from "./hubTableView";
import type { AiFitVerdict, AiSpeedEstimate, HubModel } from "@platform/lib/api";
import type { HubFamily } from "./hubFamilies";

// Every cell rule the dense table draws a column from, tested as a pure
// function — the density that makes this table worth having is also what
// makes a wrong number louder, so every cell whose source can be absent has
// its dash pinned here directly, per the plan's own warning about llmfit's
// own `search` table (columns filled entirely with "-").

function model(id: string, extra: Partial<HubModel> = {}): HubModel {
  return {
    id,
    task: "text generation",
    taskHelp: null,
    pipelineTag: "text-generation",
    capability: "text-generation",
    gated: null,
    library: null,
    downloads: null,
    likes: null,
    updated: null,
    params: null,
    estimatedSize: null,
    fit: null,
    speedEstimate: null,
    created: null,
    baseModel: null,
    relation: null,
    local: { state: "none" },
    url: `https://huggingface.co/${id}`,
    ...extra,
  };
}

describe("ageLabel", () => {
  it("reads a real created date as a compact age", () => {
    const created = new Date(Date.now() - 18 * 86400 * 1000).toISOString();
    expect(ageLabel(created)).toBe("18d ago");
  });

  it("is a dash, never an invented age, when the Hub did not say", () => {
    expect(ageLabel(null)).toBe("—");
  });

  it("is a dash rather than a NaN for an unparseable date", () => {
    expect(ageLabel("not a date")).toBe("—");
  });
});

describe("fitCell", () => {
  const verdict = (score: number, v: AiFitVerdict["verdict"]): AiFitVerdict => ({
    verdict: v,
    basis: "download",
    footprintBytes: 1e9,
    score,
  });

  it("carries the bar percent and the three-way dot together", () => {
    expect(fitCell(verdict(60, "easy"))).toEqual({ percent: 60, dot: "easy" });
    expect(fitCell(verdict(31, "tight"))).toEqual({ percent: 31, dot: "tight" });
    expect(fitCell(verdict(0, "no"))).toEqual({ percent: 0, dot: "no" });
  });

  it("is null — not a zero-width bar — when nothing was judged", () => {
    expect(fitCell(null)).toBeNull();
  });
});

describe("speedLabel", () => {
  const estimate = (tokensPerSecond: number): AiSpeedEstimate => ({
    tokensPerSecond,
    method: "bandwidth",
    backend: "metal-mlx",
    bandwidthGbS: 200,
    contextTokens: 8192,
    calibrated: false,
    calibrationFactor: null,
  });

  it("shows one decimal of tok/s", () => {
    expect(speedLabel(estimate(24.13))).toBe("24.1");
  });

  it("is a dash, never '0 tok/s', when there is no estimate", () => {
    // The exact llmfit failure mode this plan calls out by name: a search
    // table filled with `-` is useless, but a search table showing "0" for
    // "we do not know" is actively wrong, not merely unhelpful.
    expect(speedLabel(null)).toBe("—");
  });
});

describe("runModeLabel", () => {
  it("names the three run modes in words a reader chose, not the wire strings", () => {
    expect(runModeLabel("gpu")).toBe("GPU");
    expect(runModeLabel("cpu-offload")).toBe("CPU offload");
    expect(runModeLabel("cpu-only")).toBe("CPU only");
  });

  it("is a dash when there is no fit to read a run mode off of", () => {
    expect(runModeLabel(undefined)).toBe("—");
  });
});

describe("popLabel", () => {
  it("compacts a download count the same way the rest of the page does", () => {
    expect(popLabel(117_000)).toBe("117K");
    expect(popLabel(54_321)).toBe("54K");
    expect(popLabel(42)).toBe("42");
  });

  it("is a dash, never a 0, for a repo the Hub reported no count for", () => {
    expect(popLabel(null)).toBe("—");
  });
});

describe("variantLabel", () => {
  it("states the count once there is more than one repo in the family", () => {
    expect(variantLabel(4)).toBe("4");
    expect(variantLabel(31)).toBe("31");
  });

  it("is a dash for a family that is just the one repo — never a bare 0", () => {
    expect(variantLabel(0)).toBe("—");
  });
});

describe("familyDisplay", () => {
  function family(primary: HubModel, variants: HubModel[] = []): HubFamily {
    return { key: primary.baseModel ?? primary.id, primary, variants };
  }

  it("names the family by its base model, with the ACTUAL variant offered on the line under it", () => {
    // The thing a Download button acts on is the primary repo id, which may
    // not equal the family's own display name — the mock's whole point.
    const primary = model("mlx-community/Qwen3.8-27B-4bit", { baseModel: "Qwen/Qwen3.8-27B" });
    const display = familyDisplay(family(primary, [model("x")]));
    expect(display.name).toBe("Qwen/Qwen3.8-27B");
    expect(display.variantId).toBe("mlx-community/Qwen3.8-27B-4bit");
  });

  it("shows no redundant second line for a standalone repo with no base model", () => {
    const primary = model("tencent/Hy4-preview");
    const display = familyDisplay(family(primary));
    expect(display.name).toBe("tencent/Hy4-preview");
    expect(display.variantId).toBeNull();
  });
});
