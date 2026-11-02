import { expect, test } from "bun:test";

import { CANVAS_SHOT_SCRIM_HINT, canvasShotScrimLabel } from "./canvas-shot-scrim";

test("the user's own capture is a plain progress line, per target", () => {
  expect(
    canvasShotScrimLabel({ what: "canvas", by: "user", udf: null, waitingForFrame: false }),
  ).toBe("Capturing the canvas…");
  expect(
    canvasShotScrimLabel({ what: "udf", by: "user", udf: null, waitingForFrame: false }),
  ).toBe("Capturing this UDF's output…");
  expect(
    canvasShotScrimLabel({ what: "udf", by: "user", udf: "nyc311_hex", waitingForFrame: false }),
  ).toBe("Capturing nyc311_hex's output…");
});

test("the agent's capture names the agent — nobody pressed anything", () => {
  expect(
    canvasShotScrimLabel({ what: "canvas", by: "agent", udf: null, waitingForFrame: false }),
  ).toBe("Your agent is looking at the canvas…");
  expect(
    canvasShotScrimLabel({ what: "udf", by: "agent", udf: "ports_dashboard", waitingForFrame: false }),
  ).toBe("Your agent is looking at ports_dashboard's output…");
});

test("before the workbench's first hello the line says it is waiting, not capturing", () => {
  expect(
    canvasShotScrimLabel({ what: "canvas", by: "agent", udf: null, waitingForFrame: true }),
  ).toBe("Waiting for the workbench…");
});

test("the hint tells the user why the canvas is not answering", () => {
  expect(CANVAS_SHOT_SCRIM_HINT).toMatch(/hold still/i);
});
