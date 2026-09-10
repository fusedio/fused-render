// THE COUNT DOES NOT BLINK on the way back to the landing (P4-08b / C G-20b).
//
// T re-reads the artifacts index on every path onto the landing page but
// deliberately does NOT reset the count first: "both reads are about to run
// again and land within a few hundred ms, and clearing them first would take the
// tab bar off screen and put it back for the trip — a stale count for a moment
// is quieter than a section that blinks. Boot is the only place the 'not read
// yet' state is real." (T:13049-13053.)
//
// Natively the count lives in this hook, inside `Home`, which unmounts on the
// way into a chat — so `null` came back on every Back.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { Artifact } from "../protocol/artifacts";

const { useArtifacts, resetArtifactsMemoryForTests } = await import(
  "./useArtifacts"
);

const reads: string[] = [];
let answer: Artifact[] = [];
const read = (_dir: string, file: string | null) => {
  reads.push(file ?? "");
  return Promise.resolve(answer);
};

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  resetArtifactsMemoryForTests();
  reads.length = 0;
  answer = [];
});

async function mount(file: string | null) {
  let out: Artifact[] | null = null;
  const seen: Array<Artifact[] | null> = [];
  function Probe() {
    out = useArtifacts("/tpl", file, read as never);
    seen.push(out);
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Probe));
  });
  for (let i = 0; i < 3; i++) await act(async () => {});
  mounted.push(r);
  return {
    rows: () => out,
    /** Every value this mount ever published, oldest first. */
    seen,
    unmount() {
      act(() => r.unmount());
      mounted.splice(mounted.indexOf(r), 1);
    },
  };
}

const A: Artifact[] = [{ remote_url: "https://x.test/a", title: "A" }];

test("BOOT is the only place `null` is real", async () => {
  answer = A;
  const first = await mount("/repo/x.py");
  expect(first.seen[0]).toBe(null);
  expect(first.rows()?.length).toBe(1);
});

test("Back holds the previous rows while the fresh read is in flight", async () => {
  answer = A;
  const first = await mount("/repo/x.py");
  first.unmount();

  // The trip into a chat and out again. The read still runs — the artifacts
  // INDEX is the one call that leaves the machine, and a published page is
  // exactly what a turn just changed — but the tab bar over the rows must not
  // go off screen and come back for it.
  const back = await mount("/repo/x.py");
  expect(back.seen[0]).not.toBe(null);
  expect(back.seen.every((v) => v !== null)).toBe(true);
  expect(reads.length).toBe(2);
  expect(back.rows()?.length).toBe(1);
});

test("a DIFFERENT target has no rows to hold, so it shows none", async () => {
  answer = A;
  const first = await mount("/repo/x.py");
  first.unmount();
  answer = [];
  const other = await mount("/repo/y.py");
  // Never the other file's list: the memory is per target.
  expect(other.seen[0]).toBe(null);
  expect(other.rows()).toEqual([]);
});

test("an emptied list really empties: `[]` replaces the remembered rows", async () => {
  answer = A;
  const first = await mount("/repo/x.py");
  first.unmount();
  answer = [];
  const back = await mount("/repo/x.py");
  // Held through the read, then honestly replaced — the section goes away.
  expect(back.seen[0]?.length).toBe(1);
  expect(back.rows()).toEqual([]);
});
