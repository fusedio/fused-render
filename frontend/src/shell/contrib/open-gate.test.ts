// The app page's open gate for a Marketplace copy (open-gate.ts): a soft
// block's 423 asks instead of stopping, and a known Marketplace copy's frame
// waits for the first status read.
import { expect, test } from "bun:test";
import type { AppContrib, AppContribState, CopyBlock } from "@platform/lib/contrib";
import { asksFirst, openRefusal, overviewGate } from "./open-gate";

const SOFT: CopyBlock = { level: "soft", reason: "It sends your files somewhere.", what_to_do: "Wait for a fix." };
const HARD: CopyBlock = { ...SOFT, level: "hard" };

test("a 423 with the block's facts is a refusal; anything else is not", () => {
  expect(openRefusal({ status: 423, body: { blocked: SOFT } })).toEqual(SOFT);
  expect(openRefusal({ status: 423, body: { blocked: HARD } })).toEqual(HARD);
  expect(openRefusal({ status: 423, body: {} })).toBeNull();
  expect(openRefusal({ status: 500, body: { blocked: HARD } })).toBeNull();
  expect(openRefusal(new Error("network"))).toBeNull();
  expect(openRefusal(null)).toBeNull();
});

test("a soft refusal asks first; a hard one stops", () => {
  expect(asksFirst(SOFT)).toBe(true);
  expect(asksFirst(HARD)).toBe(false);
});

function state(block: CopyBlock | null, settled: boolean): AppContribState {
  const data = block ? ({ panel: { block } } as unknown as AppContrib) : null;
  return { data, settled, answered: settled, refresh: async () => {} };
}

test("the Overview holds a Marketplace copy's frame until its first status read settles", () => {
  expect(overviewGate(state(null, false), true)).toBe("hold");
  expect(overviewGate(state(null, true), true)).toBe("frame");
});

test("a folder not known to be a Marketplace copy frames at once", () => {
  expect(overviewGate(state(null, false), false)).toBe("frame");
});

test("a soft block that waits asks; once said yes to, the app frames", () => {
  expect(overviewGate(state(SOFT, true), true)).toBe("ask");
  expect(overviewGate(state({ ...SOFT, confirmed_at: 1 }, true), true)).toBe("frame");
  expect(overviewGate(state({ ...SOFT, confirmed: true }, true), true)).toBe("frame");
});
