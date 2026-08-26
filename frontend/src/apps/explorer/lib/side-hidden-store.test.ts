import { beforeEach, describe, expect, it } from "bun:test";
import { getSideHidden, setSideHidden } from "./side-hidden-store";

// The module-variable design IS the refresh guarantee (see the file's header):
// there is no storage to clear, so every test here resets the flag by hand,
// the same way a real refresh would reset it by reloading the document.
beforeEach(() => setSideHidden(false));

describe("side-hidden-store", () => {
  it("starts clean", () => {
    expect(getSideHidden()).toBe(false);
  });

  it("remembers a close", () => {
    setSideHidden(true);
    expect(getSideHidden()).toBe(true);
  });

  it("clears on a reopen", () => {
    setSideHidden(true);
    setSideHidden(false);
    expect(getSideHidden()).toBe(false);
  });
});
