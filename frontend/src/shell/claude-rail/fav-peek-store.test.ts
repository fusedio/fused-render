// The shell's favourite card and the page's rails (fav-peek-store): a stack
// of rails, the last one still mounted is the one the card stands beside;
// a keyboard press's wish to focus the card waits for the card's key.
import { afterEach, describe, expect, it } from "bun:test";
import {
  PENDING_FOCUS_MS,
  favPeekState,
  noteFavOpened,
  onFavOpened,
  registerRail,
  requestFavFocus,
  resetFavPeekStoreForTests,
  takeFavFocus,
} from "./fav-peek-store";

const rail = (name: string) => ({ name, isConnected: true }) as unknown as HTMLElement;
const settle = () => new Promise<void>((res) => queueMicrotask(res));

afterEach(() => resetFavPeekStoreForTests());

describe("the rail stack", () => {
  it("the last registered rail is the card's", async () => {
    const a = rail("a");
    const b = rail("b");
    registerRail(a);
    registerRail(b);
    await settle();
    expect(favPeekState().rail).toBe(b);
  });

  it("a page hop's late cleanup — the OLD rail leaving after the new one came — keeps the new one", async () => {
    const a = rail("a");
    const b = rail("b");
    const offA = registerRail(a);
    registerRail(b);
    offA();
    await settle();
    expect(favPeekState().rail).toBe(b);
  });

  it("the top rail leaving hands the card back to the one under it, and the last one to none", async () => {
    const a = rail("a");
    const b = rail("b");
    const offA = registerRail(a);
    const offB = registerRail(b);
    offB();
    await settle();
    expect(favPeekState().rail).toBe(a);
    offA();
    await settle();
    expect(favPeekState().rail).toBeNull();
  });

  it("skips a rail that is off the page without having said so", async () => {
    const a = rail("a");
    const b = { isConnected: false } as unknown as HTMLElement;
    registerRail(a);
    registerRail(b);
    await settle();
    expect(favPeekState().rail).toBe(a);
  });
});

describe("the pending focus", () => {
  it("is taken once, by the card standing on that key", async () => {
    requestFavFocus("k1");
    await settle();
    expect(favPeekState().pendingFocus).toBe("k1");
    expect(takeFavFocus("k2")).toBe(false);
    expect(takeFavFocus("k1")).toBe(true);
    await settle();
    expect(favPeekState().pendingFocus).toBeNull();
    expect(takeFavFocus("k1")).toBe(false);
  });

  it("a withdrawn wish (the open write refused) is not taken", () => {
    requestFavFocus("k1");
    requestFavFocus(null);
    expect(takeFavFocus("k1")).toBe(false);
  });

  it("a stale wish is spent without taking the keyboard", () => {
    const realNow = Date.now;
    try {
      requestFavFocus("k1");
      const t0 = realNow();
      Date.now = () => t0 + PENDING_FOCUS_MS + 1;
      expect(takeFavFocus("k1")).toBe(false);
      expect(takeFavFocus("k1")).toBe(false);
    } finally {
      Date.now = realNow;
    }
  });
});

describe("an open answered open", () => {
  it("is heard at once by every listener, with its key, until it unsubscribes", () => {
    const heard: string[] = [];
    const off = onFavOpened((k) => heard.push(k));
    noteFavOpened("k1");
    expect(heard).toEqual(["k1"]);
    off();
    noteFavOpened("k2");
    expect(heard).toEqual(["k1"]);
  });
});
