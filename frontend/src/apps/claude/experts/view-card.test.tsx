// An expert's `show` row as a card (docs/experts.md §14): which tool rows are cards, that a card never
// folds behind "more", that a canvas card mounts nothing, and that a node card loads in place only when new.
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";

import { groupCollapsibles } from "../protocol/segments";
import type { Segment, ToolSegment } from "../protocol/types";
import { ViewCard } from "../ui/ViewCard";
import { canvasToolLabel, viewCardOf } from "./view-card";

const payload = { kind: "widget", path: "/c/prod/user/Mine/chart.json", title: "Sales by month", canvas: "Mine", env: "prod", note: "Monthly sales", live: true };
const tool = (over: Partial<ToolSegment>): ToolSegment =>
  ({ kind: "tool", id: "t1", name: "Bash", input: {}, status: "ok", output: "", images: [], ...over }) as ToolSegment;
const show = (over: Partial<ToolSegment> = {}) =>
  tool({ name: "mcp__expert_canvas__show", output: "The card is in the chat.\n[fused-card] " + JSON.stringify(payload), ...over });

describe("viewCardOf", () => {
  test("reads the card line of a settled show", () => {
    expect(viewCardOf(show())).toEqual(payload);
    expect(viewCardOf(show({ name: "mcp__expert-canvas__show" }))).toEqual(payload);
  });
  test("a Windows server's path is a card too", () => {
    const at = (path: string) => viewCardOf(show({ output: "[fused-card] " + JSON.stringify({ ...payload, path }) }));
    const win = "C:\\Users\\u\\Fused\\canvas\\prod\\user\\Mine\\chart.json";
    expect(at(win)).toEqual({ ...payload, path: win });
    expect(at("C:/Users/u/chart.json")?.path).toBe("C:/Users/u/chart.json");
    expect(at("\\\\host\\share\\chart.json")?.path).toBe("\\\\host\\share\\chart.json");
    // The title falls back to the file's name whichever separator the path uses.
    expect(viewCardOf(show({ output: "[fused-card] " + JSON.stringify({ kind: "widget", path: win }) }))?.title).toBe("chart.json");
    expect(at("C:chart.json")).toBeNull();
  });
  test("anything else stays a tool row", () => {
    expect(viewCardOf(show({ status: "running", output: null }))).toBeNull();
    expect(viewCardOf(show({ status: "error" }))).toBeNull();
    expect(viewCardOf(show({ output: "error: no such file" }))).toBeNull();
    expect(viewCardOf(show({ output: "[fused-card] {not json" }))).toBeNull();
    expect(viewCardOf(show({ output: '[fused-card] {"kind":"udf","path":"relative.py"}' }))).toBeNull();
    // Another tool printing the same line (a Bash that cats a transcript) is not a card.
    expect(viewCardOf(tool({ output: show().output }))).toBeNull();
    expect(viewCardOf({ kind: "text", text: "x" } as Segment)).toBeNull();
  });
});

test("a card splits the fold instead of hiding behind it", () => {
  const segs: Segment[] = [tool({ id: "a" }), tool({ id: "b" }), show({ id: "c" }), tool({ id: "d" }), tool({ id: "e" })];
  const rows = groupCollapsibles(segs);
  expect(rows.map((r) => r.kind)).toEqual(["run", "seg", "run"]);
  expect(rows[1]).toMatchObject({ kind: "seg", index: 2 });
  // A failed show folds like any other tool row.
  expect(groupCollapsibles([tool({ id: "a" }), show({ id: "c", status: "error" }), tool({ id: "d" })]).map((r) => r.kind)).toEqual(["run"]);
});

test("the two tools read as plain words", () => {
  expect(canvasToolLabel("mcp__expert_canvas__canvas_create", { name: "Parcels" }, true)).toBe("Made the canvas Parcels");
  expect(canvasToolLabel("mcp__expert_canvas__canvas_create", {}, false)).toBe("Making the canvas");
  expect(canvasToolLabel("mcp__expert_canvas__show", {}, false)).toBe("Showing a card");
  expect(canvasToolLabel("Bash", {}, true)).toBeNull();
});

test("a canvas card is one row with an Open button and no frame", () => {
  const html = renderToStaticMarkup(<ViewCard view={{ ...payload, kind: "canvas" }} live />);
  expect(html).toContain(">Open</button>");
  expect(html).not.toContain("<iframe");
  expect(html).not.toContain("c-viewcard-box");
});

test("a node card from history asks before it loads; one born in a live turn loads in the same box", () => {
  const stale = renderToStaticMarkup(<ViewCard view={payload} id="s1" />);
  expect(stale).toContain("Sales by month");
  expect(stale).toContain("Monthly sales · Widget · in Mine · prod");
  expect(stale).toContain("Load live");
  expect(stale).toContain("c-viewcard-box");
  expect(stale).not.toContain("<iframe");
  const fresh = renderToStaticMarkup(<ViewCard view={payload} id="s2" live />);
  expect(fresh).toContain("c-viewcard-box");
  expect(fresh).toContain("<iframe");
  expect(fresh).not.toContain("Load live");
  // The expert held it back (`show` with live: false): it waits for a click even in a live turn.
  const heavy = renderToStaticMarkup(<ViewCard view={{ ...payload, live: false }} id="s3" live />);
  expect(heavy).toContain("Load live");
  expect(heavy).not.toContain("<iframe");
  expect(viewCardOf(show({ output: "x\n[fused-card] " + JSON.stringify({ ...payload, live: false }) }))?.live).toBe(false);
});
