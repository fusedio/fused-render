// The three specialist task tools read as plain words in the tool row and the
// status line, and every other tool keeps its own name.
import { expect, test } from "bun:test";

import { specialistToolLabel } from "./tool-labels";
import { activityDetail, activityVerb } from "../ui/WorkingLine";

const T = (t: string) => `mcp__fused_render__specialist_${t}`;

test("the three labels name the specialist from the tool input", () => {
  expect(specialistToolLabel(T("pull"), "Canvas", true)).toBe("Used the Canvas specialist");
  expect(specialistToolLabel(T("pull"), undefined, true)).toBe("Looked up the specialists");
  expect(specialistToolLabel(T("note"), "Canvas", true)).toBe("Sent a note to the Canvas specialist");
  expect(specialistToolLabel(T("breakout"), "Canvas", true)).toBe("Breakout with the Canvas specialist");
});

test("tools that are not the three get no label", () => {
  expect(specialistToolLabel(T("list"), "Canvas", true)).toBeNull();
  expect(specialistToolLabel("mcp__fused_render__s3_read", "Canvas", true)).toBeNull();
});

test("the status line says it while it runs, without the name repeated as detail", () => {
  const stats = {
    phase: "tooling",
    activity: { tool: { name: T("note"), detail: "Canvas" } },
  } as unknown as Parameters<typeof activityVerb>[0];
  expect(activityVerb(stats, "Thinking", 0)).toBe("Sending a note to the Canvas specialist");
  expect(activityDetail(stats)).toBe("");
});
