// The rail card's flag (rail-peek-flag): off until the prefs read says on, off
// on an absent field (an older server), and a publish beats a slower in-flight
// read. Its trigger: click unless the prefs say hover.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useCanvasRailPeek, useCanvasRailPeekTrigger, publishCanvasRailPeek, resetCanvasRailPeekFlagForTests } =
  await import("./rail-peek-flag");

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

async function probe<T = boolean>(use: () => T = useCanvasRailPeek as () => T) {
  const seen: T[] = [];
  function Probe() {
    seen.push(use());
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
    publishCanvasRailPeek({ enabled: true });
  });
  await act(async () => {
    release();
    await new Promise((res) => setTimeout(res, 0));
  });
  expect(seen[seen.length - 1]).toBe(true);
});

test("the trigger is click by default and on an older server, hover only when the prefs say so", async () => {
  answer = async () => ({ canvas_rail_peek: { enabled: true } });
  let seen = await probe(useCanvasRailPeekTrigger);
  expect(seen.every((v) => v === "click")).toBe(true);
  resetCanvasRailPeekFlagForTests();
  answer = async () => ({ canvas_rail_peek: { enabled: true, trigger: "hover" } });
  seen = await probe(useCanvasRailPeekTrigger);
  expect(seen[0]).toBe("click");
  expect(seen[seen.length - 1]).toBe("hover");
  resetCanvasRailPeekFlagForTests();
  answer = async () => ({ canvas_rail_peek: { enabled: true, trigger: "press" } });
  seen = await probe(useCanvasRailPeekTrigger);
  expect(seen[seen.length - 1]).toBe("click");
});

test("a published trigger reaches a mounted rail without touching the switch", async () => {
  answer = async () => ({ canvas_rail_peek: { enabled: true, trigger: "click" } });
  const triggers = await probe(useCanvasRailPeekTrigger);
  const enabled = await probe();
  await act(async () => {
    publishCanvasRailPeek({ enabled: true, trigger: "hover" });
  });
  expect(triggers[triggers.length - 1]).toBe("hover");
  expect(enabled[enabled.length - 1]).toBe(true);
});
