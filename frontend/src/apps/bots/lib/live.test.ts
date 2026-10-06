import { describe, expect, test } from "bun:test";
import {
  furlTarget, imessageStatus, isUrl, keyParams, modelChips, nextDown, rankUsage, routineLabel, showUrl, toPageXY, weighted, type KeyLike,
} from "./live";

const rect = { left: 100, top: 50, width: 640, height: 400 };

describe("toPageXY", () => {
  test("frame metadata wins: the viewport is shorter than the window", () => {
    // 1280x720 CSS px shown at 640x400: x scales by 2, y by 1.8.
    expect(toPageXY(420, 250, rect, [1280, 800], { deviceWidth: 1280, deviceHeight: 720 }, [1280, 800])).toEqual({ x: 640, y: 360 });
  });
  test("falls back to the bot's viewport, then the image's natural size", () => {
    expect(toPageXY(420, 250, rect, [1920, 1200], null, [1280, 800])).toEqual({ x: 640, y: 400 });
    expect(toPageXY(420, 250, rect, [1920, 1200], null, null)).toEqual({ x: 960, y: 600 });
  });
  test("null outside the page or before the frame has a size", () => {
    expect(toPageXY(90, 250, rect, [1280, 800], null, [1280, 800])).toBeNull();
    expect(toPageXY(420, 460, rect, [1280, 800], null, [1280, 800])).toBeNull();
    expect(toPageXY(420, 250, rect, [0, 0], null, [1280, 800])).toBeNull();
    expect(toPageXY(420, 250, { ...rect, width: 0 }, [1280, 800], null, [1280, 800])).toBeNull();
  });
  test("rounds to whole pixels", () => {
    expect(toPageXY(100.3, 50.3, rect, [1280, 800], null, [1280, 800])).toEqual({ x: 1, y: 1 });
  });
});

describe("nextDown", () => {
  test("counts clicks within 400 ms and 6 px", () => {
    const a = nextDown({ t: 0, x: 0, y: 0, n: 0 }, 1000, { x: 10, y: 10 });
    expect(a.n).toBe(1);
    const b = nextDown(a, 1300, { x: 12, y: 13 });
    expect(b.n).toBe(2);
    expect(nextDown(b, 1800, { x: 12, y: 13 }).n).toBe(1);   // too slow
    expect(nextDown(b, 1400, { x: 30, y: 13 }).n).toBe(1);   // too far
  });
});

const k = (key: string, mods: Partial<KeyLike> = {}, type = "keydown"): KeyLike =>
  ({ type, key, code: "", altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...mods });

describe("keyParams", () => {
  test("printable keys carry text and a virtual key code", () => {
    const p = keyParams(k("a"));
    expect(p).toMatchObject({ type: "keyDown", key: "a", text: "a", unmodifiedText: "a", windowsVirtualKeyCode: 65, modifiers: 0 });
    expect(p.commands).toBeUndefined();
    expect(keyParams(k("A", { shiftKey: true }))).toMatchObject({ text: "A", modifiers: 8 });
  });
  test("keyup carries no text", () => {
    const p = keyParams(k("a", {}, "keyup"));
    expect(p.type).toBe("keyUp");
    expect(p.text).toBeUndefined();
  });
  test("Enter sends a carriage return", () => {
    expect(keyParams(k("Enter"))).toMatchObject({ text: "\r", windowsVirtualKeyCode: 13 });
  });
  test("editing commands", () => {
    expect(keyParams(k("a", { metaKey: true })).commands).toEqual(["SelectAll"]);
    expect(keyParams(k("a", { metaKey: true })).text).toBeUndefined();
    expect(keyParams(k("z", { metaKey: true })).commands).toEqual(["Undo"]);
    expect(keyParams(k("z", { metaKey: true, shiftKey: true })).commands).toEqual(["Redo"]);
    expect(keyParams(k("c", { ctrlKey: true })).commands).toEqual(["Copy"]);
    expect(keyParams(k("ArrowLeft", { metaKey: true })).commands).toEqual(["MoveToBeginningOfLine"]);
    expect(keyParams(k("Backspace", { metaKey: true })).commands).toEqual(["DeleteToBeginningOfLine"]);
    expect(keyParams(k("ArrowLeft", { ctrlKey: true })).commands).toBeUndefined();
  });
  test("autoRepeat follows the event", () => {
    expect(keyParams(k("ArrowDown", { repeat: true }))).toMatchObject({ autoRepeat: true, windowsVirtualKeyCode: 40 });
  });
});

describe("URL bar", () => {
  test("isUrl: schemes, dotted hosts and localhost are addresses", () => {
    for (const u of ["https://example.com", "chrome://settings", "example.com", "example.com/path?q=1", "localhost", "localhost:3000/x", "a.b:8080"]) expect(isUrl(u)).toBe(true);
  });
  test("isUrl: bare words and phrases go to Google", () => {
    for (const u of ["weather", "best pizza near me", "how to example.com", "foo/bar"]) expect(isUrl(u)).toBe(false);
    expect(furlTarget("best pizza")).toBe("https://www.google.com/search?q=best%20pizza");
    expect(furlTarget("example.com")).toBe("example.com");
  });
  test("showUrl hides blank pages", () => {
    expect(showUrl("about:blank")).toBe("");
    expect(showUrl(undefined)).toBe("");
    expect(showUrl("https://x.y")).toBe("https://x.y");
  });
});

describe("routineLabel", () => {
  test("interval", () => expect(routineLabel({ kind: "interval", minutes: 60 })).toBe("every 60 min"));
  test("daily lists weekdays unless every day", () => {
    expect(routineLabel({ kind: "daily", time: "09:00", weekdays: [0, 1, 2, 3, 4] })).toBe("daily at 09:00 (Mon Tue Wed Thu Fri)");
    expect(routineLabel({ kind: "daily", time: "07:30", weekdays: [0, 1, 2, 3, 4, 5, 6] })).toBe("daily at 07:30");
  });
  test("once", () => {
    expect(routineLabel({ kind: "once", at: 1_700_000_000 })).toMatch(/^once at .+/);
    expect(routineLabel({ kind: "once" })).toBe("once at —");
  });
});

describe("usage ranking", () => {
  test("weighted: model weights, unknown models count as sonnet", () => {
    expect(weighted({ models: { haiku: 10, opus: 2, local: 3 } })).toBeCloseTo(3 + 10 + 3);
    expect(weighted({ models: { "local-4b": 50 } })).toBe(0);
    expect(weighted({})).toBe(0);
  });
  test("modelChips: most calls first", () => {
    expect(modelChips({ haiku: 2, opus: 9, sonnet: 5 })).toEqual([["opus", 9], ["sonnet", 5], ["haiku", 2]]);
    expect(modelChips(null)).toEqual([]);
  });
  test("rankUsage: live first, then weighted spend, then today", () => {
    const rows: { id: string; live: boolean; today: number; models: Record<string, number> }[] = [
      { id: "gone", live: false, today: 99, models: { opus: 99 } },
      { id: "cheap", live: true, today: 50, models: { haiku: 50 } },   // 15
      { id: "pricey", live: true, today: 4, models: { opus: 4 } },     // 20
      { id: "tie", live: true, today: 20, models: { haiku: 50 } },     // 15, fewer today than cheap
    ];
    expect(rankUsage(rows).map((r) => r.id)).toEqual(["pricey", "cheap", "tie", "gone"]);
  });
});

describe("imessageStatus", () => {
  const s = { running: true, error: "", last_in: 1000 - 120, last_out: null };
  test("each bridge state", () => {
    expect(imessageStatus("", s)).toMatch(/^Off\./);
    expect(imessageStatus("+1", null)).toBe("Bridge status unknown yet.");
    expect(imessageStatus("+1", { ...s, error: "no access" })).toBe("Bridge not running: no access");
    expect(imessageStatus("+1", { ...s, running: false })).toBe("Bridge starting…");
    expect(imessageStatus("+1", s, 1000 * 1000)).toBe("Bridge running · last text in 2 min ago, last reply out never.");
  });
});
