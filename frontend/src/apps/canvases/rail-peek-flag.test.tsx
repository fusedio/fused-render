// The rail hover card's flag (rail-peek-flag): off until the prefs read says
// on, off on an absent field (an older server), and a publish beats a slower
// in-flight read.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useCanvasRailPeek, publishCanvasRailPeekEnabled, resetCanvasRailPeekFlagForTests } = await import(
  "./rail-peek-flag"
);

let answer: () => Promise<unknown> = async () => ({});
const realFetch = globalThis.fetch;
beforeEach(() => {
  resetCanvasRailPeekFlagForTests();
  (globalThis as { fetch: unknown }).fetch = async () => {
    const body = await answer();
    return { ok: true, json: async () => body } as unknown as Response;
  };
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetCanvasRailPeekFlagForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

async function probe() {
  const seen: boolean[] = [];
  function Probe() {
    seen.push(useCanvasRailPeek());
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(<Probe />);
  });
  mounted.push(r);
  await act(async () => {
    await new Promise((res) => setTimeout(res, 0));
  });
  return seen;
}

test("off by default, on once the prefs say so", async () => {
  answer = async () => ({ canvas_rail_peek: { enabled: true } });
  const seen = await probe();
  expect(seen[0]).toBe(false);
  expect(seen[seen.length - 1]).toBe(true);
});

test("an older server (no field) reads as off", async () => {
  answer = async () => ({ canvases: { enabled: true } });
  const seen = await probe();
  expect(seen.every((v) => v === false)).toBe(true);
});

test("a publish wins over a slower in-flight read", async () => {
  let release!: () => void;
  answer = () =>
    new Promise((res) => {
      release = () => res({ canvas_rail_peek: { enabled: false } });
    });
  const seen = await probe();
  await act(async () => {
    publishCanvasRailPeekEnabled(true);
  });
  await act(async () => {
    release();
    await new Promise((res) => setTimeout(res, 0));
  });
  expect(seen[seen.length - 1]).toBe(true);
});
