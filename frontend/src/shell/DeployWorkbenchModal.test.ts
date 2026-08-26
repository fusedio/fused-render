import { describe, expect, test } from "bun:test";
import { canvasNameForApp } from "./workbench-deploy-lib";


describe("canvasNameForApp", () => {
  test("keeps a Workbench-safe app name unchanged", () => {
    expect(canvasNameForApp("terrain_viewer_2")).toBe("terrain_viewer_2");
  });

  test("turns spaces and punctuation into one safe Canvas name", () => {
    expect(canvasNameForApp("  My terrain-viewer!  ")).toBe("My_terrain_viewer");
  });

  test("always returns a usable non-empty fallback", () => {
    expect(canvasNameForApp("✨")).toBe("Fused_App");
  });
});
