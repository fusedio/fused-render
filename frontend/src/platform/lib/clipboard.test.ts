// `copyPendingText`: a clipboard write whose text is still being fetched. The
// write has to START inside the click (WebKit drops the user activation the
// moment the handler awaits anything), so the pending text goes in as a
// promise-valued ClipboardItem rather than being awaited first. See the doc
// comment in clipboard.ts.
import { afterEach, expect, test } from "bun:test";

import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { copyPendingText } from "@platform/lib/clipboard";

type Nav = { clipboard?: unknown };
type Glob = { ClipboardItem?: unknown };
const nav = navigator as unknown as Nav;
const glob = globalThis as unknown as Glob;

class FakeClipboardItem {
  constructor(public parts: Record<string, Blob | Promise<Blob>>) {}
}

afterEach(() => {
  delete nav.clipboard;
  delete glob.ClipboardItem;
});

test("with ClipboardItem, the write is issued synchronously, before the text resolves", async () => {
  let written: FakeClipboardItem[] | null = null;
  glob.ClipboardItem = FakeClipboardItem;
  nav.clipboard = {
    write: (items: FakeClipboardItem[]) => {
      written = items;
      return Promise.resolve();
    },
    writeText: () => {
      throw new Error("writeText must not be used when ClipboardItem exists");
    },
  };
  let resolveText!: (s: string) => void;
  const pending = new Promise<string>((r) => (resolveText = r));

  const result = copyPendingText(pending);
  // Already handed to the clipboard — no await has happened yet.
  expect(written).not.toBeNull();
  const part = written![0].parts["text/plain"];
  resolveText("claude --resume abc");
  const blob = await part;
  expect(await blob.text()).toBe("claude --resume abc");
  expect(await result).toBe(true);
});

test("without ClipboardItem, falls back to writeText with the resolved text", async () => {
  const writes: string[] = [];
  nav.clipboard = {
    writeText: (s: string) => {
      writes.push(s);
      return Promise.resolve();
    },
  };
  expect(await copyPendingText(Promise.resolve("claude"))).toBe(true);
  expect(writes).toEqual(["claude"]);
});

test("a rejected text promise reports false and does not throw", async () => {
  glob.ClipboardItem = FakeClipboardItem;
  nav.clipboard = {
    write: (items: FakeClipboardItem[]) =>
      // Real browsers reject the write when the item's promise rejects.
      Promise.resolve(items[0].parts["text/plain"]).then(() => undefined),
  };
  expect(await copyPendingText(Promise.reject(new Error("fetch failed")))).toBe(false);
});

test("a denied write reports false", async () => {
  glob.ClipboardItem = FakeClipboardItem;
  nav.clipboard = { write: () => Promise.reject(new Error("NotAllowedError")) };
  expect(await copyPendingText(Promise.resolve("claude"))).toBe(false);
});

test("no Clipboard API at all reports false", async () => {
  expect(await copyPendingText(Promise.resolve("claude"))).toBe(false);
});
