// The rail extras' flag (rail-extras-flag): not answered until the prefs read
// says so (rendered as off), off on an absent field (an older server) and on
// the retired switches it replaced, a publish beats a slower in-flight read,
// and a reader outside React hears every change.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useRailExtras, useRailExtrasAnswer, publishRailExtras, railExtrasNow, resetRailExtrasFlagForTests, subscribeRailExtras } =
  await import("./rail-extras-flag");

let answer: () => Promise<unknown> = async () => ({});
const realFetch = globalThis.fetch;
beforeEach(() => {
  resetRailExtrasFlagForTests();
  (globalThis as { fetch: unknown }).fetch = async () => {
    const body = await answer();
    return { ok: true, json: async () => body } as unknown as Response;
  };
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetRailExtrasFlagForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

async function probe() {
  const seen: { known: boolean | null; on: boolean }[] = [];
  function Probe() {
    seen.push({ known: useRailExtrasAnswer(), on: useRailExtras() });
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

test("not answered at first (off, and known as null); on once the read says so", async () => {
  answer = async () => ({ rail_extras: { enabled: true } });
  const seen = await probe();
  expect(seen[0]).toEqual({ known: null, on: false });
  expect(seen[seen.length - 1]).toEqual({ known: true, on: true });
});

test("an absent field (an older server) is a known off", async () => {
  answer = async () => ({});
  const seen = await probe();
  expect(seen[seen.length - 1]).toEqual({ known: false, on: false });
});

test("the three switches it replaced turn nothing on", async () => {
  answer = async () => ({
    rail_mode_doors: { enabled: true },
    rail_expand: { enabled: true },
    rail_free_pins: { enabled: true },
  });
  const seen = await probe();
  expect(seen[seen.length - 1]).toEqual({ known: false, on: false });
  expect(railExtrasNow()).toBe(false);
});

test("a reader outside React hears the switch, and stops hearing it once it leaves", () => {
  let heard = 0;
  const leave = subscribeRailExtras(() => (heard += 1));
  publishRailExtras({ enabled: true });
  expect(railExtrasNow()).toBe(true);
  expect(heard).toBe(1);
  // The same answer again is not a change.
  publishRailExtras({ enabled: true });
  expect(heard).toBe(1);
  leave();
  publishRailExtras({ enabled: false });
  expect(railExtrasNow()).toBe(false);
  expect(heard).toBe(1);
});

test("a publish beats a slower in-flight read", async () => {
  let release!: (v: unknown) => void;
  answer = () => new Promise((res) => (release = res));
  const seen = await probe();
  await act(async () => {
    publishRailExtras({ enabled: true });
  });
  expect(seen[seen.length - 1]).toEqual({ known: true, on: true });
  await act(async () => {
    release({ rail_extras: { enabled: false } });
    await new Promise((res) => setTimeout(res, 0));
  });
  expect(seen[seen.length - 1]).toEqual({ known: true, on: true });
});
