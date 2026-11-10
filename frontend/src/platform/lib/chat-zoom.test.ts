// The chat text zoom's pure parts (chat-zoom.ts): the chord, the step, and
// which presses belong to a chat. The CSS half (every px font-size multiplied
// by `--c-zoom`) is read off the sheets in apps/claude/styles/parity.test.ts.
import { beforeEach, describe, expect, it } from "bun:test";
import { _setUiLayoutForTest, getUiLayout } from "./ui-layout";

const {
  _resetChatZoomForTest,
  chatZoomChord,
  getChatTextScale,
  handleChatZoomKey,
  isInChat,
  stepChatTextScale,
} = await import("./chat-zoom");
const { isMac } = await import("./platform");

const MOD = { metaKey: isMac, ctrlKey: !isMac, altKey: false, shiftKey: false };
const key = (k: string, code: string, extra: Partial<typeof MOD> = {}) => ({ key: k, code, ...MOD, ...extra });

/** A fake element: `closest` answers from the selectors its ancestry matches. */
function el(matches: string[]): { closest(sel: string): unknown } {
  const self = {
    closest(sel: string) {
      return sel.split(",").some((s) => matches.includes(s.trim())) ? self : null;
    },
  };
  return self;
}

describe("chatZoomChord", () => {
  it("reads ⌘= / ⌘+ / ⌘− / ⌘0 by character or physical key", () => {
    expect(chatZoomChord(key("=", "Equal"))).toBe(1);
    expect(chatZoomChord(key("+", "Equal", { shiftKey: true }))).toBe(1);
    expect(chatZoomChord(key("+", "NumpadAdd"))).toBe(1);
    expect(chatZoomChord(key("-", "Minus"))).toBe(-1);
    expect(chatZoomChord(key("-", "NumpadSubtract"))).toBe(-1);
    expect(chatZoomChord(key("0", "Digit0"))).toBe(0);
    expect(chatZoomChord(key("0", "Numpad0"))).toBe(0);
  });
  it("ignores the chord without the primary modifier, with ⌥, or ⇧0", () => {
    expect(chatZoomChord({ ...key("=", "Equal"), metaKey: false, ctrlKey: false })).toBeNull();
    expect(chatZoomChord(key("=", "Equal", { altKey: true }))).toBeNull();
    expect(chatZoomChord(key(")", "Digit0", { shiftKey: true }))).toBeNull();
    expect(chatZoomChord({ ...key("=", "Equal"), metaKey: true, ctrlKey: true })).toBeNull();
    expect(chatZoomChord(key("a", "KeyA"))).toBeNull();
  });
});

describe("stepChatTextScale", () => {
  it("walks the step list and clamps at both ends", () => {
    expect(stepChatTextScale(1, 1)).toBe(1.1);
    expect(stepChatTextScale(1.1, 1)).toBe(1.25);
    expect(stepChatTextScale(1, -1)).toBe(0.9);
    expect(stepChatTextScale(2, 1)).toBe(2);
    expect(stepChatTextScale(0.8, -1)).toBe(0.8);
    expect(stepChatTextScale(1.75, 0)).toBe(1);
  });
  it("snaps an off-list value before stepping", () => {
    expect(stepChatTextScale(1.3, 1)).toBe(1.5);
    expect(stepChatTextScale(5, -1)).toBe(1.75);
  });
});

describe("isInChat", () => {
  it("is a chat root, a chat popup, never a compact tile, never the shell", () => {
    expect(isInChat(el([".chat-root"]))).toBe(true);
    expect(isInChat(el([".c-tokens"]))).toBe(true);
    expect(isInChat(el([".c-overlay"]))).toBe(true);
    expect(isInChat(el([".chat-root", ".chat-compact"]))).toBe(false);
    expect(isInChat(el([".sidebar"]))).toBe(false);
    expect(isInChat(null)).toBe(false);
    expect(isInChat({})).toBe(false);
  });
});

describe("handleChatZoomKey", () => {
  beforeEach(() => {
    _setUiLayoutForTest({ v: 1 });
    _resetChatZoomForTest(1);
  });
  const press = (target: unknown, k = "=", code = "Equal") => {
    let prevented = false;
    const handled = handleChatZoomKey({
      ...key(k, code),
      target: target as EventTarget,
      preventDefault: () => {
        prevented = true;
      },
    });
    return { handled, prevented };
  };

  it("inside a chat: steps, claims the press and persists the factor", () => {
    expect(press(el([".chat-root"]))).toEqual({ handled: true, prevented: true });
    expect(getChatTextScale()).toBe(1.1);
    expect(getUiLayout().chatTextScale).toBe(1.1);
    press(el([".c-overlay"]), "-", "Minus");
    press(el([".c-overlay"]), "-", "Minus");
    expect(getChatTextScale()).toBe(0.9);
    press(el([".chat-root"]), "0", "Digit0");
    expect(getChatTextScale()).toBe(1);
    expect(getUiLayout().chatTextScale).toBe(1);
  });
  it("outside a chat: untouched and not claimed", () => {
    expect(press(el([".sidebar"]))).toEqual({ handled: false, prevented: false });
    expect(press(el([".chat-root", ".chat-compact"]))).toEqual({ handled: false, prevented: false });
    expect(press(null)).toEqual({ handled: false, prevented: false });
    expect(getChatTextScale()).toBe(1);
    expect(getUiLayout().chatTextScale).toBeUndefined();
  });
});
