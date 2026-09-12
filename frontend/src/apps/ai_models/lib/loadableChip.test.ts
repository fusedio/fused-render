import { describe, expect, test } from "bun:test";
import { loadableChipText } from "./loadableChip";

describe("loadableChipText", () => {
  test("builds the chip text from the server's reason clause", () => {
    expect(loadableChipText({ loadable: false, loadableReason: "mflux only loads FLUX.2 Klein" })).toBe(
      "Won't run here · mflux only loads FLUX.2 Klein",
    );
    expect(
      loadableChipText({ loadable: false, loadableReason: "neo_chat not supported by mlx-vlm" }),
    ).toBe("Won't run here · neo_chat not supported by mlx-vlm");
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
