// The expandable rail's flag (rail-expand-flag): not answered until the prefs
// read says so (rendered as off), off on an absent field (an older server),
// and a publish beats a slower in-flight read.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useRailExpand, useRailExpandAnswer, publishRailExpand, resetRailExpandFlagForTests } = await import(
  "./rail-expand-flag"
);

let answer: () => Promise<unknown> = async () => ({});
const realFetch = globalThis.fetch;
beforeEach(() => {
  resetRailExpandFlagForTests();
  (globalThis as { fetch: unknown }).fetch = async () => {
    const body = await answer();
    return { ok: true, json: async () => body } as unknown as Response;
  };
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetRailExpandFlagForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

async function probe() {
  const seen: { known: boolean | null; on: boolean }[] = [];
  function Probe() {
    seen.push({ known: useRailExpandAnswer(), on: useRailExpand() });
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
  answer = async () => ({ rail_expand: { enabled: true } });
  const seen = await probe();
  expect(seen[0]).toEqual({ known: null, on: false });
  expect(seen[seen.length - 1]).toEqual({ known: true, on: true });
});

test("an absent field (an older server) is a known off", async () => {
  answer = async () => ({});
  const seen = await probe();
  expect(seen[seen.length - 1]).toEqual({ known: false, on: false });
});

test("a publish beats a slower in-flight read", async () => {
  let release!: (v: unknown) => void;
  answer = () => new Promise((res) => (release = res));
  const seen = await probe();
  await act(async () => {
    publishRailExpand({ enabled: true });
  });
  expect(seen[seen.length - 1]).toEqual({ known: true, on: true });
  await act(async () => {
    release({ rail_expand: { enabled: false } });
    await new Promise((res) => setTimeout(res, 0));
  });
  expect(seen[seen.length - 1]).toEqual({ known: true, on: true });
});
