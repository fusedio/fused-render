// The file view's picture of the node it shows (SPEC §53.8): what it takes,
// what it refuses, and what it says about the picture.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";

import type { CaptureResult } from "@apps/claude/shots/types";

const { NESTED_BLANK_CAVEAT, nodePageShot } = await import("./node-shot");

const frame = {} as HTMLIFrameElement;
const taken = (over: Partial<CaptureResult> = {}): CaptureResult => ({
  blob: new Blob(["x"], { type: "image/png" }),
  width: 10,
  height: 10,
  notes: [],
  via: "native",
  ...over,
});

test("pictures its own node from the frame on screen, and says what the picture is", async () => {
  const seen: HTMLIFrameElement[] = [];
  const shot = nodePageShot(() => frame, "udf_1", async (f) => {
    seen.push(f);
    return taken({ notes: ["a canvas inside it is blank"] });
  });
  expect(shot.targets).toEqual(["udf"]);
  // Named, or left to the page: both are this page's node.
  for (const udf of ["udf_1", undefined]) {
    const a = await shot.capture("udf", udf, "agent");
    expect(a.ok).toBe(true);
    if (a.ok) {
      expect(a.framing).toBe("node");
      expect(a.note).toBe('this is "udf_1" as its own file shows it in the file view; a canvas inside it is blank');
    }
  }
  expect(seen).toEqual([frame, frame]);
});

test("refuses the graph and any other node without taking a picture", async () => {
  let takes = 0;
  const shot = nodePageShot(() => frame, "udf_1", async () => {
    takes += 1;
    return taken();
  });
  for (const [what, udf] of [["canvas", undefined], ["udf", "udf_2"]] as const) {
    const a = await shot.capture(what, udf, "agent");
    expect(a).toEqual({ ok: false, error: 'this page shows only "udf_1", as its own file', refusal: true });
  }
  expect(takes).toBe(0);
});

test("no frame on screen, or a capture that came back empty, is an error with its reason", async () => {
  const none = await nodePageShot(() => null, "udf_1", async () => taken()).capture("udf");
  expect(none).toEqual({ ok: false, error: '"udf_1" is not on screen in the file view' });
  const empty = await nodePageShot(() => frame, "udf_1", async () =>
    taken({ blob: null, via: "none", why: "the pane could not be read" }),
  ).capture("udf");
  expect(empty).toEqual({ ok: false, error: 'the page of "udf_1" could not be captured (the pane could not be read)' });
});

test("a picture redrawn from the page's own document says its result area is blank", async () => {
  const dom = await nodePageShot(() => frame, "udf_1", async () => taken({ via: "dom" })).capture("udf");
  expect(dom.ok && dom.note).toBe(`this is "udf_1" as its own file shows it in the file view; ${NESTED_BLANK_CAVEAT}`);
  const native = await nodePageShot(() => frame, "udf_1", async () => taken({ via: "native" })).capture("udf");
  expect(native.ok && native.note).toBe('this is "udf_1" as its own file shows it in the file view');
});
