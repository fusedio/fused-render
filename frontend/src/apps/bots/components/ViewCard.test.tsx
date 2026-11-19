// An expert's `show` card (docs/experts.md §17): what the row's payload becomes, and that the collapsed card
// mounts nothing of the work itself.
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ViewCard, canvasFrameUrl, canvasPageUrl, viewOf, viewSubtitle } from "./ViewCard";

const widget = { kind: "widget", path: "/c/prod/user/Mine/chart.json", title: "Sales by month", canvas: "Mine", env: "prod" };

describe("viewOf", () => {
  test("keeps a whole payload and fills a missing title from the file", () => {
    expect(viewOf(widget)).toEqual(widget);
    expect(viewOf({ kind: "udf", path: "/c/prod/user/Mine/load.py" })).toEqual({ kind: "udf", path: "/c/prod/user/Mine/load.py", title: "load.py", canvas: "", env: "" });
  });
  test("refuses what is not a view of an absolute path", () => {
    for (const bad of [null, undefined, "x", {}, { kind: "udf" }, { kind: "udf", path: "relative/x.py" }, { path: "/a/b.py" }]) expect(viewOf(bad)).toBeNull();
  });
});

describe("the card", () => {
  test("names the kind, the canvas and the environment", () => {
    expect(viewSubtitle(widget)).toBe("Widget · in Mine · prod");
    expect(viewSubtitle({ ...widget, kind: "canvas", title: "Mine", path: "/c/prod/user/Mine" })).toBe("Canvas · prod");
    expect(viewSubtitle({ ...widget, kind: "other" })).toBe("File · in Mine · prod");
  });
  test("a canvas opens as its own page, chrome-free in the overlay", () => {
    const v = { ...widget, kind: "canvas", canvas: "My_Canvas" };
    expect(canvasFrameUrl(v)).toBe("/canvases/prod/My_Canvas?embed=1&side=off");
    expect(canvasPageUrl(v)).toBe("/canvases/prod/My_Canvas");
  });
  test("collapsed, it is one row with an Open button and no frame", () => {
    const html = renderToStaticMarkup(<ViewCard view={widget} />);
    expect(html).toContain("Sales by month");
    expect(html).toContain("Widget · in Mine · prod");
    expect(html).toContain(">Open</button>");
    expect(html).not.toContain("<iframe");
    expect(html).not.toContain("node-view");
  });
});
