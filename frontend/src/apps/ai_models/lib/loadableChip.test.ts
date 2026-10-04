import { describe, expect, test } from "bun:test";
import { loadableChipText, runsOnChipText } from "./loadableChip";

describe("loadableChipText", () => {
  test("builds the chip text from the server's reason clause", () => {
    expect(loadableChipText({ loadable: false, loadableReason: "mflux only loads FLUX.2 Klein" })).toBe(
      "Won't run here · mflux only loads FLUX.2 Klein",
    );
    expect(
      loadableChipText({ loadable: false, loadableReason: "neo_chat not supported by mlx-vlm" }),
    ).toBe("Won't run here · neo_chat not supported by mlx-vlm");
  });

  test("D1288's architecture-named reason shapes", () => {
    expect(
      loadableChipText({
        loadable: false,
        loadableReason: "needs Diffusers (MiniMaxH3Pipeline)",
      }),
    ).toBe("Won't run here · needs Diffusers (MiniMaxH3Pipeline)");
    expect(loadableChipText({ loadable: false, loadableReason: "needs Diffusers" })).toBe(
      "Won't run here · needs Diffusers",
    );
    expect(
      loadableChipText({
        loadable: false,
        loadableReason: "needs Sentence Transformers — not supported yet",
      }),
    ).toBe("Won't run here · needs Sentence Transformers — not supported yet");
    expect(
      loadableChipText({ loadable: false, loadableReason: "no engine loads SomeWeirdArch yet" }),
    ).toBe("Won't run here · no engine loads SomeWeirdArch yet");
    expect(loadableChipText({ loadable: false, loadableReason: "no engine here loads this" })).toBe(
      "Won't run here · no engine here loads this",
    );
  });

  test("falls back to a bare label when loadable is false with no reason", () => {
    expect(loadableChipText({ loadable: false, loadableReason: null })).toBe("Won't run here");
    expect(loadableChipText({ loadable: false })).toBe("Won't run here");
  });

  test("null when the row is loadable, or the field is absent (older server)", () => {
    expect(loadableChipText({ loadable: true, loadableReason: null })).toBeNull();
    expect(loadableChipText({})).toBeNull();
  });
});

describe("runsOnChipText", () => {
  test("names the engine the active runner would need to switch to", () => {
    expect(runsOnChipText({ runsOnEngine: "Diffusers" })).toBe("Runs on Diffusers");
    expect(runsOnChipText({ runsOnEngine: "MLX FLUX" })).toBe("Runs on MLX FLUX");
  });

  test("null when there is no other engine to name", () => {
    expect(runsOnChipText({ runsOnEngine: null })).toBeNull();
    expect(runsOnChipText({ runsOnEngine: undefined })).toBeNull();
    expect(runsOnChipText({})).toBeNull();
  });
});
