// The split's GEOMETRY, tested as the pure functions it is factored into — the
// clamp, the default, the pointer→percent conversion and the "write no inline
// width" rule are the parts a re-implementation gets subtly wrong.
import { describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const { clampPct, pctFromPointer, splitPctFromParam, splitWidth, SPLIT_MAX, SPLIT_MIN } = await import(
  "./useSplit"
);
const { narrowViewOf, viewClassNames, viewToggleLabel } = await import("./useNarrowView");
const { leftBarShown, pickerHost } = await import("./LeftModePicker");

describe("clampPct", () => {
  test("clamps to the columns' minimum useful widths", () => {
    expect(clampPct(0)).toBe(SPLIT_MIN);
    expect(clampPct(19.9)).toBe(SPLIT_MIN);
    expect(clampPct(100)).toBe(SPLIT_MAX);
    expect(clampPct(50)).toBe(50);
  });
});

describe("splitPctFromParam", () => {
  test("unset is 70", () => {
    expect(splitPctFromParam(undefined)).toBe(70);
    expect(splitPctFromParam("")).toBe(70);
    expect(splitPctFromParam(null)).toBe(70);
  });
  test("an out-of-range bookmark is clamped, not rejected", () => {
    expect(splitPctFromParam("5")).toBe(SPLIT_MIN);
    expect(splitPctFromParam("95")).toBe(SPLIT_MAX);
  });
  test("garbage reads as the default rather than as NaN%", () => {
    expect(splitPctFromParam("wide")).toBe(70);
  });
  test("a fractional value survives (the divider writes toFixed(1))", () => {
    expect(splitPctFromParam("62.5")).toBe(62.5);
  });
});

describe("pctFromPointer", () => {
  test("the pointer's share of the window, clamped", () => {
    expect(pctFromPointer(640, 1280)).toBe(50);
    expect(pctFromPointer(10, 1280)).toBe(SPLIT_MIN);
    expect(pctFromPointer(1270, 1280)).toBe(SPLIT_MAX);
  });
  test("what the mouseup writes is the same number, to one decimal", () => {
    expect(pctFromPointer(800, 1280).toFixed(1)).toBe("62.5");
  });
});

describe("splitWidth — the inline declaration, and when there must not be one", () => {
  test("wide: the param's percentage", () => {
    expect(splitWidth("62.5", false, false)).toBe("62.5%");
    expect(splitWidth(undefined, false, false)).toBe("70%");
  });
  test("narrow: NO inline width — the stylesheet owns the column, and inline would win", () => {
    expect(splitWidth("62.5", true, false)).toBeUndefined();
  });
  test("no pane: no ratio to be OF", () => {
    expect(splitWidth("62.5", false, true)).toBeUndefined();
  });
  test("a live drag beats the param, and is clamped the same way", () => {
    expect(splitWidth("70", false, false, 33.3)).toBe("33.3%");
    expect(splitWidth("70", false, false, 5)).toBe("20%");
  });
  test("the PARAM is never touched by the collapse: the same param, wide again, is the user's ratio", () => {
    const param = "62.5";
    expect(splitWidth(param, true, false)).toBeUndefined();
    expect(splitWidth(param, false, false)).toBe("62.5%");
  });
});

describe("narrowViewOf — param, then a live crossing, then the default", () => {
  test("unset reads as the CHAT: a narrow pane opens on the conversation", () => {
    expect(narrowViewOf(undefined, null)).toBe("chat");
  });
  test("a crossing keeps the preview that was already on screen", () => {
    expect(narrowViewOf(undefined, "preview")).toBe("preview");
  });
  test("a param, once present, wins over the crossing in BOTH directions", () => {
    expect(narrowViewOf("chat", "preview")).toBe("chat");
    expect(narrowViewOf("preview", null)).toBe("preview");
  });
  test("an unknown value reads as the chat rather than as an error", () => {
    expect(narrowViewOf("sideways", null)).toBe("chat");
  });
});

describe("view classes and the toggle's one string", () => {
  test("the classes go on the chat root, and a no-pane target gets none", () => {
    expect(viewClassNames(true, "preview", false)).toBe("narrow view-preview");
    expect(viewClassNames(false, "chat", false)).toBe("view-chat");
    expect(viewClassNames(true, "preview", true)).toBe("");
  });
  test("the button names its DESTINATION", () => {
    expect(viewToggleLabel("chat")).toBe("Comment on preview");
    expect(viewToggleLabel("preview")).toBe("Back to chat");
  });
});

describe("pickerHost — where the picker lives is a layout question", () => {
  test("wide is the pane's own bar, narrow is the shared strip", () => {
    expect(pickerHost(false, false, 3)).toBe("leftbar");
    expect(pickerHost(true, false, 3)).toBe("anntools");
  });
  test("a one-item picker is chrome that cannot do anything", () => {
    expect(pickerHost(false, false, 1)).toBe("none");
    expect(pickerHost(false, false, 0)).toBe("none");
  });
  test("no pane, no hosts", () => {
    expect(pickerHost(false, true, 3)).toBe("none");
  });
  test("the bar follows the picker: no choice, no empty bordered strip", () => {
    expect(leftBarShown(false, false, 3)).toBe(true);
    expect(leftBarShown(false, false, 1)).toBe(false);
    expect(leftBarShown(true, false, 3)).toBe(false);
  });
});
