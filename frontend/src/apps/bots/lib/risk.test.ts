import { describe, expect, test } from "bun:test";

import { severityLabel } from "./risk";

describe("severityLabel", () => {
  test("names the three levels", () => {
    expect(severityLabel("low")).toBe("Low risk");
    expect(severityLabel("medium")).toBe("Medium risk");
    expect(severityLabel("high")).toBe("High risk");
  });
  test("anything else is no pill", () => {
    for (const v of [undefined, null, "", "scary", "toString", 3]) expect(severityLabel(v)).toBeNull();
  });
});
