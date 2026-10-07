// Bare "/" → the Claude chat (lib/chat-focus.ts): who takes the key, who is
// left alone, and the open-the-pane handshake.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, it } from "bun:test";

import {
  OPEN_CHAT_EVENT,
  armFocusOnRegister,
  focusAnyChatComposer,
  handleSlashKey,
  isEditableTarget,
  registerChatComposer,
  requestChatPane,
  resetChatFocusForTests,
} from "./chat-focus";
import { acquireOverlay, releaseOverlay } from "./ui-overlay";

// Patched onto the shared shim document per test and put back after — never
// replaced or deleted (other suites in the run read the same object).
type Doc = { activeElement: unknown; dispatchEvent?: (e: Event) => boolean };
const doc = (globalThis as unknown as { document: Doc }).document;
const saved = { active: doc.activeElement, dispatch: doc.dispatchEvent };
let bus: EventTarget;

beforeEach(() => {
  resetChatFocusForTests();
  bus = new EventTarget();
  doc.dispatchEvent = (e: Event) => bus.dispatchEvent(e);
  doc.activeElement = null;
});
afterEach(() => {
  resetChatFocusForTests();
  doc.activeElement = saved.active;
  if (saved.dispatch) doc.dispatchEvent = saved.dispatch;
  else delete doc.dispatchEvent;
});

function slash(init: Partial<KeyboardEvent> = {}) {
  let prevented = false;
  const e = {
    key: "/",
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    isComposing: false,
    defaultPrevented: false,
    preventDefault() {
      prevented = true;
    },
    ...init,
  } as unknown as KeyboardEvent;
  return { e, prevented: () => prevented };
}

describe("registerChatComposer / focusAnyChatComposer", () => {
  it("focuses the newest on-screen chat first", () => {
    const calls: string[] = [];
    registerChatComposer(() => (calls.push("old"), true));
    registerChatComposer(() => (calls.push("new"), true));
    expect(focusAnyChatComposer()).toBe(true);
    expect(calls).toEqual(["new"]);
  });

  it("falls back past a chat whose box is off screen", () => {
    const calls: string[] = [];
    registerChatComposer(() => (calls.push("old"), true));
    registerChatComposer(() => (calls.push("hidden"), false));
    expect(focusAnyChatComposer()).toBe(true);
    expect(calls).toEqual(["hidden", "old"]);
  });

  it("an unregistered chat is never asked", () => {
    let asked = false;
    const off = registerChatComposer(() => (asked = true));
    off();
    expect(focusAnyChatComposer()).toBe(false);
    expect(asked).toBe(false);
  });

  it("an armed focus is taken by the next registration, once", () => {
    armFocusOnRegister();
    let n = 0;
    registerChatComposer(() => (n++, true));
    expect(n).toBe(1);
    registerChatComposer(() => (n++, true));
    expect(n).toBe(1);
  });

  it("nothing armed, nothing focused on register", () => {
    let n = 0;
    registerChatComposer(() => (n++, true));
    expect(n).toBe(0);
  });
});

describe("requestChatPane", () => {
  it("is true only when a host calls preventDefault", () => {
    expect(requestChatPane()).toBe(false);
    bus.addEventListener(OPEN_CHAT_EVENT, (e) => e.preventDefault());
    expect(requestChatPane()).toBe(true);
  });
});

describe("isEditableTarget", () => {
  it("inputs, textareas, selects and contenteditable are editable", () => {
    for (const tagName of ["INPUT", "TEXTAREA", "SELECT"]) {
      expect(isEditableTarget({ tagName } as Element)).toBe(true);
    }
    expect(isEditableTarget({ tagName: "DIV", isContentEditable: true } as unknown as Element)).toBe(true);
    expect(isEditableTarget({ tagName: "BUTTON" } as Element)).toBe(false);
    expect(isEditableTarget(null)).toBe(false);
  });
});

describe("handleSlashKey", () => {
  it("focuses an on-screen chat and takes the key", () => {
    registerChatComposer(() => true);
    const k = slash();
    expect(handleSlashKey(k.e)).toBe(true);
    expect(k.prevented()).toBe(true);
  });

  it("asks a host to open the pane when no chat is on screen", () => {
    bus.addEventListener(OPEN_CHAT_EVENT, (e) => e.preventDefault());
    const k = slash();
    expect(handleSlashKey(k.e)).toBe(true);
    expect(k.prevented()).toBe(true);
  });

  it("leaves the key alone when there is nothing to focus or open", () => {
    const k = slash();
    expect(handleSlashKey(k.e)).toBe(false);
    expect(k.prevented()).toBe(false);
  });

  it("a slash typed into an editable field stays there", () => {
    registerChatComposer(() => true);
    doc.activeElement = { tagName: "TEXTAREA" };
    const k = slash();
    expect(handleSlashKey(k.e)).toBe(false);
    expect(k.prevented()).toBe(false);
  });

  it("ignores modified, composing, already-handled and other keys", () => {
    registerChatComposer(() => true);
    for (const init of [
      { metaKey: true },
      { ctrlKey: true },
      { altKey: true },
      { isComposing: true },
      { defaultPrevented: true },
      { key: "a" },
    ]) {
      expect(handleSlashKey(slash(init).e)).toBe(false);
    }
  });

  it("stands down while an overlay holds the keyboard", () => {
    registerChatComposer(() => true);
    acquireOverlay();
    try {
      expect(handleSlashKey(slash().e)).toBe(false);
    } finally {
      releaseOverlay();
    }
  });
});
