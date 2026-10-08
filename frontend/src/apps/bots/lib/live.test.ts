import { describe, expect, test } from "bun:test";
import {
  furlTarget, heldButton, imessageStatus, isUrl, keyAction, modelChips, nextDown, rankUsage, routineLabel, showUrl, toPageXY, toStageBox, typesText, weighted, wrapIndex, type KeyLike,
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

const k = (key: string, mods: Partial<KeyLike> = {}, type = "keydown", code = ""): KeyLike =>
  ({ type, key, code, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...mods });
/** The dispatchKeyEvent params of an event that is one. */
const keyParams = (e: KeyLike) => { const a = keyAction(e); return a?.kind === "key" ? a.params : undefined; };

describe("keyParams", () => {
  test("printable keys carry text and a virtual key code", () => {
    const p = keyParams(k("a"))!;
    expect(p).toMatchObject({ type: "keyDown", key: "a", text: "a", unmodifiedText: "a", windowsVirtualKeyCode: 65, modifiers: 0 });
    expect(p.commands).toBeUndefined();
    expect(keyParams(k("A", { shiftKey: true }))).toMatchObject({ text: "A", modifiers: 8 });
  });
  test("keyup carries no text", () => {
    const p = keyParams(k("a", {}, "keyup"))!;
    expect(p.type).toBe("keyUp");
    expect(p.text).toBeUndefined();
  });
  test("Enter sends a carriage return", () => {
    expect(keyParams(k("Enter"))).toMatchObject({ text: "\r", windowsVirtualKeyCode: 13 });
  });
  test("editing commands", () => {
    expect(keyParams(k("a", { metaKey: true }))!.commands).toEqual(["SelectAll"]);
    expect(keyParams(k("a", { metaKey: true }))!.text).toBeUndefined();
    expect(keyParams(k("a", { metaKey: true }))!.type).toBe("rawKeyDown");
    expect(keyParams(k("z", { metaKey: true }))!.commands).toEqual(["Undo"]);
    expect(keyParams(k("z", { metaKey: true, shiftKey: true }))!.commands).toEqual(["Redo"]);
    expect(keyParams(k("c", { ctrlKey: true }))!.commands).toEqual(["Copy"]);
    expect(keyParams(k("ArrowLeft", { metaKey: true }))!.commands).toEqual(["MoveToBeginningOfLine"]);
    expect(keyParams(k("ArrowLeft", { metaKey: true, shiftKey: true }))!.commands).toEqual(["MoveToBeginningOfLineAndModifySelection"]);
    expect(keyParams(k("ArrowLeft", { altKey: true }))!.commands).toEqual(["MoveWordLeft"]);
    expect(keyParams(k("Backspace", { metaKey: true }))!.commands).toEqual(["DeleteToBeginningOfLine"]);
    expect(keyParams(k("Home"))!.commands).toEqual(["MoveToBeginningOfLine"]);
  });
  test("punctuation gets the US-layout virtual key, never the character code", () => {
    // ord(".") is 46 = VK_DELETE: a forwarder that sends it deletes the next character instead of typing a dot.
    expect(keyParams(k(".", {}, "keydown", "Period"))).toMatchObject({ text: ".", windowsVirtualKeyCode: 190 });
    expect(keyParams(k("'", {}, "keydown", "Quote"))).toMatchObject({ text: "'", windowsVirtualKeyCode: 222 });
    expect(keyParams(k(">", { shiftKey: true }, "keydown", "Period"))).toMatchObject({ text: ">", windowsVirtualKeyCode: 190 });
    expect(keyParams(k("-"))).toMatchObject({ text: "-", windowsVirtualKeyCode: 189 });  // code missing: found by character
    expect(keyParams(k("1"))).toMatchObject({ text: "1", windowsVirtualKeyCode: 49 });
  });
  test("the viewer's own keyCode wins for named keys", () => {
    expect(keyParams(k("Enter", { keyCode: 13 }))!.windowsVirtualKeyCode).toBe(13);
    expect(keyParams(k("F5", { keyCode: 116 }))!.windowsVirtualKeyCode).toBe(116);
  });
  test("characters outside the US table are inserted as text, their keyup dropped", () => {
    expect(keyAction(k("é", {}, "keydown", "KeyE"))).toEqual({ kind: "insert", text: "é" });
    expect(keyAction(k("日", {}, "keydown", ""))).toEqual({ kind: "insert", text: "日" });
    expect(keyAction(k("🙂", {}, "keydown", ""))).toEqual({ kind: "insert", text: "🙂" });
    expect(keyAction(k("👨‍👩‍👧", {}, "keydown", ""))).toEqual({ kind: "insert", text: "👨‍👩‍👧" });  // a sequence, not a named key
    expect(keyAction(k("é", {}, "keyup", "KeyE"))).toBeNull();
  });
  test("never sets nativeVirtualKeyCode (it hides the tab on macOS)", () => {
    for (const e of [k("a"), k("Shift", { shiftKey: true, keyCode: 16 }), k(".", {}, "keydown", "Period")]) {
      expect("nativeVirtualKeyCode" in (keyParams(e) as object)).toBe(false);
    }
  });
  test("autoRepeat follows the event", () => {
    expect(keyParams(k("ArrowDown", { repeat: true }))).toMatchObject({ autoRepeat: true, windowsVirtualKeyCode: 40 });
  });
});

describe("stage geometry and small predicates", () => {
  test("toStageBox scales the page rect to the drawn frame and offsets by the image's place and the stage's scroll", () => {
    // frame covers 1280x720 CSS px, drawn at 640x360 at (100, 50) inside a stage at (20, 10), stage scrolled 0/30
    const img = { left: 100, top: 50, width: 640, height: 360 }, stage = { left: 20, top: 10, width: 800, height: 600 };
    expect(toStageBox([200, 100, 400, 40], img, stage, [1280, 720], { x: 0, y: 30 })).toEqual({ left: 180, top: 120, width: 200, height: 20 });
    expect(toStageBox([0, 0, 0, 0], img, stage, [1280, 720])).toEqual({ left: 80, top: 40, width: 0, height: 0 });
  });
  test("heldButton names the button Chrome needs on a move", () => {
    expect(heldButton(0)).toBe("none"); expect(heldButton(1)).toBe("left"); expect(heldButton(2)).toBe("right"); expect(heldButton(4)).toBe("middle"); expect(heldButton(3)).toBe("left");
  });
  test("wrapIndex wraps both ways and survives an empty list", () => {
    expect(wrapIndex(0, -1, 3)).toBe(2); expect(wrapIndex(2, 1, 3)).toBe(0); expect(wrapIndex(0, 1, 0)).toBe(0);
  });
  test("typesText: letters and Backspace refresh suggestions, chords and navigation do not", () => {
    expect(typesText(k("a"))).toBe(true); expect(typesText(k("Backspace"))).toBe(true);
    for (const e of [k("a", { metaKey: true }), k("ArrowDown"), k("Tab"), k("Enter"), k("F5"), k("Shift", { shiftKey: true })]) expect(typesText(e)).toBe(false);
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
