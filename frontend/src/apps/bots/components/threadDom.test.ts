// tableToCsv's DOM reader is a one-liner; the quoting rules live in the pure rowsToCsv (bun tests have no DOM).
import { afterEach, describe, expect, test } from "bun:test";

import { copyCsv, rowsToCsv } from "./threadDom";

describe("copyCsv", () => {
  const orig = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  afterEach(() => {
    if (orig) Object.defineProperty(globalThis, "navigator", orig);
    else delete (globalThis as { navigator?: unknown }).navigator;
  });
  const stub = (clipboard: unknown) => Object.defineProperty(globalThis, "navigator", { value: { clipboard }, configurable: true, writable: true });
  const tick = () => new Promise((r) => setTimeout(r, 0));

  test("label is Copied only once the write resolves", async () => {
    const wrote: string[] = [];
    stub({ writeText: async (s: string) => { wrote.push(s); } });
    const btn = { textContent: "Copy as CSV" };
    copyCsv(btn, "a,b");
    expect(btn.textContent).toBe("Copy as CSV");
    await tick();
    expect(wrote).toEqual(["a,b"]);
    expect(btn.textContent).toBe("Copied");
  });
  test("a rejected write, or no clipboard, reads Copy failed", async () => {
    stub({ writeText: () => Promise.reject(new Error("denied")) });
    const a = { textContent: "Copy as CSV" };
    copyCsv(a, "x");
    await tick();
    expect(a.textContent).toBe("Copy failed");
    stub(undefined);
    const b = { textContent: "Copy as CSV" };
    copyCsv(b, "x");
    expect(b.textContent).toBe("Copy failed");
  });
});

describe("rowsToCsv", () => {
  test("a header and two rows", () => {
    expect(rowsToCsv([["a", "b"], ["1", "2"], ["3", "4"]])).toBe("a,b\n1,2\n3,4");
  });
  test("a cell with a comma is quoted", () => {
    expect(rowsToCsv([["x, y", "z"]])).toBe('"x, y",z');
  });
  test("a quote is doubled inside quotes; a newline is quoted", () => {
    expect(rowsToCsv([['say "hi"', "a\nb"]])).toBe('"say ""hi""","a\nb"');
  });
});
