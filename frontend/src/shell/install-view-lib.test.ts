import { describe, expect, it } from "bun:test";
import { installVerdict } from "./install-view-lib";

describe("installVerdict", () => {
  it("lists a .fused folder reached with no nav hint once stat says directory", () => {
    expect(installVerdict(null, true)).toBe("listing");
  });
  it("shows the install page for a .fused file", () => {
    expect(installVerdict(null, false)).toBe("install");
    expect(installVerdict(false, null)).toBe("install");
  });
  it("lists when the navigation hint says directory, whatever stat says", () => {
    expect(installVerdict(true, null)).toBe("listing");
    expect(installVerdict(true, false)).toBe("listing");
  });
  it("waits while the stat is in flight with no hint", () => {
    expect(installVerdict(null, null)).toBe("pending");
  });
});
