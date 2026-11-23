// The three expert task tools read as plain words in the tool row and the
// status line, and every other tool keeps its own name.
import { expect, test } from "bun:test";

import { expertToolLabel } from "./tool-labels";
import { activityDetail, activityVerb } from "../ui/WorkingLine";

const T = (t: string) => `mcp__fused_render__expert_${t}`;

test("the three labels name the expert from the tool input", () => {
  expect(expertToolLabel(T("pull"), "Canvas", true)).toBe("Used the Canvas expert");
  expect(expertToolLabel(T("pull"), undefined, true)).toBe("Looked up the experts");
  expect(expertToolLabel(T("note"), "Canvas", true)).toBe("Sent a note to the Canvas expert");
  expect(expertToolLabel(T("breakout"), "Canvas", true)).toBe("Breakout with the Canvas expert");
});

test("tools that are not the three get no label", () => {
  expect(expertToolLabel(T("list"), "Canvas", true)).toBeNull();
  expect(expertToolLabel("mcp__fused_render__s3_read", "Canvas", true)).toBeNull();
});

test("the status line says it while it runs, without the name repeated as detail", () => {
  const stats = {
    phase: "tooling",
    activity: { tool: { name: T("note"), detail: "Canvas" } },
  } as unknown as Parameters<typeof activityVerb>[0];
  expect(activityVerb(stats, "Thinking", 0)).toBe("Sending a note to the Canvas expert");
  expect(activityDetail(stats)).toBe("");
});
