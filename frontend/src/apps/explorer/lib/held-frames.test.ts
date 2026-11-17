import { describe, expect, it } from "bun:test";
import { retainedFrames } from "./held-frames";

const NODE = "udf";

describe("retainedFrames", () => {
  it("keeps only the incoming mode for ordinary modes", () => {
    expect(retainedFrames(["code", "api"], "api", NODE, false)).toEqual(["api"]);
    expect(retainedFrames(["code", "api", "reader"], "code", NODE, true)).toEqual(["code"]);
  });

  it("keeps the node view mounted behind another mode while it is offered", () => {
    expect(retainedFrames([NODE, "code"], "code", NODE, true)).toEqual([NODE, "code"]);
    // Node -> Code -> Api: Code goes, the node view stays where it was.
    expect(retainedFrames([NODE, "code", "api"], "api", NODE, true)).toEqual([NODE, "api"]);
  });

  it("never reorders: the kept pane stays at its index relative to the rest", () => {
    expect(retainedFrames(["code", NODE, "api"], "code", NODE, true)).toEqual(["code", NODE]);
  });

  it("drops the node view once it is no longer on offer (a git snapshot)", () => {
    expect(retainedFrames([NODE, "code"], "code", NODE, false)).toEqual(["code"]);
  });

  it("the active node view is kept whatever the flag says", () => {
    expect(retainedFrames(["code", NODE], NODE, NODE, false)).toEqual([NODE]);
  });
});
