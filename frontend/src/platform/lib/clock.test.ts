// The two time hooks: one shared minute ticker, one 8s backstop.
import { expect, test } from "bun:test";
import { createElement, Fragment } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { installDomShim } from "./testDomShim";

installDomShim();

const { GATE_FALLBACK_MS, useFallbackAfter, useNow } = await import("./clock");

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Mount a hook and collect every value it has returned. */
function probe<T>(hook: () => T) {
  const values: T[] = [];
  function Probe() {
    values.push(hook());
    return null;
  }
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(createElement(Probe));
  });
  return { values, unmount: () => act(() => tree.unmount()) };
}

test("useNow re-reads the clock on its own interval", async () => {
  const p = probe(() => useNow(20));
  const first = p.values[p.values.length - 1];
  await act(async () => {
    await sleep(80);
  });
  expect(p.values[p.values.length - 1]).toBeGreaterThan(first);
  p.unmount();
});

test("EVERY reader of one cadence reads the SAME instant", async () => {
  // Two rows a second apart in mounting must not flip "59m ago" to "1h ago" a
  // second apart: that is two cells of one column disagreeing.
  const seen: number[][] = [[], []];
  function Row({ into }: { into: number[] }) {
    into.push(useNow(20));
    return null;
  }
  let tree!: ReactTestRenderer;
  act(() => {
    tree = create(
      createElement(
        Fragment,
        null,
        createElement(Row, { into: seen[0], key: "a" }),
        createElement(Row, { into: seen[1], key: "b" }),
      ),
    );
  });
  await act(async () => {
    await sleep(80);
  });
  expect(seen[0][seen[0].length - 1]).toBe(seen[1][seen[1].length - 1]);
  act(() => tree.unmount());
});

test("the last reader takes the timer with it", async () => {
  const p = probe(() => useNow(20));
  p.unmount();
  const after = p.values.length;
  await sleep(80);
  // Nothing rendered after the unmount — a ticker running for nobody is a
  // wake-up a minute for the life of the page.
  expect(p.values.length).toBe(after);
});

test("useFallbackAfter is false, then true once the wait has gone on too long", async () => {
  const p = probe(() => useFallbackAfter(20));
  expect(p.values[0]).toBe(false);
  await act(async () => {
    await sleep(80);
  });
  expect(p.values[p.values.length - 1]).toBe(true);
  p.unmount();
});

test("un-armed is never late — the thing waited for arrived", async () => {
  const p = probe(() => useFallbackAfter(20, false));
  await act(async () => {
    await sleep(80);
  });
  expect(p.values.every((v) => v === false)).toBe(true);
  p.unmount();
});

test("the gate's wait is the chat frame's own 8s, one constant for both", () => {
  expect(GATE_FALLBACK_MS).toBe(8000);
});
