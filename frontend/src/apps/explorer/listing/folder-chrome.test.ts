// The bar-zone handover store. The counting is the whole of it, and the
// counting is exactly what a boolean got wrong: React mounts the incoming view
// before unmounting the outgoing one, so a folder→folder hop releases the old
// claim AFTER the new one is made.
import { afterEach, describe, expect, test } from "bun:test";
import {
  claimFolderChrome,
  folderChromeClaimed,
  resetFolderChrome,
  subscribeFolderChrome,
} from "./folder-chrome";

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
