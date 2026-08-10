// The crumb-bar handover store. The overlap handling is the whole of it, and
// the overlap is exactly what a boolean got wrong: React can hold the incoming
// view and the outgoing one at the same time, so a folder→folder hop may
// release the old claim AFTER the new one is made.
import { afterEach, describe, expect, test } from "bun:test";
import {
  claimFolderChrome,
  folderChromeClaimed,
  folderChromeSlot,
  resetFolderChrome,
  subscribeFolderChrome,
} from "./folder-chrome";

// The store only ever holds and compares the node, so a stand-in is enough
// here — these tests run without a DOM.
const node = (name: string) => ({ name }) as unknown as HTMLElement;

afterEach(() => resetFolderChrome());

describe("folder chrome claims", () => {
  test("unclaimed by default — the crumb bar keeps its zone", () => {
    expect(folderChromeClaimed()).toBe(false);
  });

  test("a claim moves the zone, its release moves it back", () => {
    const release = claimFolderChrome();
    expect(folderChromeClaimed()).toBe(true);
    release();
    expect(folderChromeClaimed()).toBe(false);
  });

  test("an overlapping mount/unmount never un-claims the live view", () => {
    const first = claimFolderChrome();
    const second = claimFolderChrome(); // the incoming folder mounts…
    first(); // …and only then does the outgoing one unmount
    expect(folderChromeClaimed()).toBe(true);
    second();
    expect(folderChromeClaimed()).toBe(false);
  });

  test("a release is idempotent — a double-invoked cleanup cannot go negative", () => {
    const release = claimFolderChrome();
    release();
    release();
    expect(folderChromeClaimed()).toBe(false);
    const other = claimFolderChrome();
    expect(folderChromeClaimed()).toBe(true);
    other();
  });

  test("no slot until something claims one — the bar stays at shell level", () => {
    expect(folderChromeSlot()).toBe(null);
    const release = claimFolderChrome();
    expect(folderChromeSlot()).toBe(null);
    release();
  });

  test("the claim carries the slot the bar portals into", () => {
    const el = node("left-column");
    const release = claimFolderChrome(el);
    expect(folderChromeSlot()).toBe(el);
    release();
    expect(folderChromeSlot()).toBe(null);
  });

  test("during an overlap the NEWEST slot wins, in either commit order", () => {
    // Incoming mounts first, outgoing then unmounts (folder→folder hop).
    const outgoing = claimFolderChrome(node("outgoing"));
    const incomingEl = node("incoming");
    const incoming = claimFolderChrome(incomingEl);
    outgoing();
    expect(folderChromeSlot()).toBe(incomingEl);
    incoming();

    // Outgoing unmounts first, incoming then mounts (scaffold→resolved swap).
    const goneEl = node("gone");
    const gone = claimFolderChrome(goneEl);
    gone();
    const nextEl = node("next");
    const next = claimFolderChrome(nextEl);
    expect(folderChromeSlot()).toBe(nextEl);
    next();
  });

  test("subscribers hear every change, and unsubscribe stops them", () => {
    let seen = 0;
    const off = subscribeFolderChrome(() => {
      seen += 1;
    });
    const release = claimFolderChrome();
    expect(seen).toBe(1);
    release();
    expect(seen).toBe(2);
    off();
    claimFolderChrome()();
    expect(seen).toBe(2);
  });
});
