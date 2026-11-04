// The favourites flag (favourites-flag): off until the prefs read says on, on
// only under its parent (the rail's preview card), a publish beats a slower
// in-flight read, and — the cross-tab fix — a prefs PUT made in ANOTHER tab
// reaches this one over the prefs channel without a reload.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useTaskFavourites, publishTaskFavourites, publishFavouritesParent, resetTaskFavouritesFlagForTests } =
  await import("./favourites-flag");
const { onPrefsWrittenElsewhere, putTaskFavouritesEnabled, resetPrefsChannelForTests } = await import("./api");

let answer: () => Promise<unknown> = async () => ({});
const realFetch = globalThis.fetch;
beforeEach(() => {
  resetTaskFavouritesFlagForTests();
  (globalThis as { fetch: unknown }).fetch = async () => {
    const body = await answer();
    return { ok: true, json: async () => body } as unknown as Response;
  };
});
const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  // This file's posts on the prefs channel must not reach another suite's
  // module (rail-peek-flag's import-time handler) — platform may not import
  // shell to reset it, so the channel itself is reset.
  resetPrefsChannelForTests();
  resetTaskFavouritesFlagForTests();
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

const BOTH_ON = { task_favourites: { enabled: true }, canvas_rail_peek: { enabled: true } };
const tick = () => new Promise((res) => setTimeout(res, 0));

async function probe() {
  const seen: boolean[] = [];
  function Probe() {
    seen.push(useTaskFavourites());
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(<Probe />);
  });
  mounted.push(r);
  await act(async () => {
    await tick();
  });
  return seen;
}

/** Wait (bounded) for the probe's latest answer to become `want`: a
 *  BroadcastChannel message lands on a later task, not synchronously. */
async function settlesTo(seen: boolean[], want: boolean) {
  for (let i = 0; i < 50 && seen[seen.length - 1] !== want; i++) {
    await act(async () => {
      await new Promise((res) => setTimeout(res, 10));
    });
  }
  expect(seen[seen.length - 1]).toBe(want);
}

test("off by default, on once the prefs say so", async () => {
  answer = async () => BOTH_ON;
  const seen = await probe();
  expect(seen[0]).toBe(false);
  expect(seen[seen.length - 1]).toBe(true);
});

test("a publish wins over a slower in-flight read", async () => {
  let release!: () => void;
  answer = () =>
    new Promise((res) => {
      release = () => res({ task_favourites: { enabled: false }, canvas_rail_peek: { enabled: true } });
    });
  const seen = await probe();
  await act(async () => {
    publishTaskFavourites({ enabled: true });
  });
  await act(async () => {
    release();
    await tick();
  });
  expect(seen[seen.length - 1]).toBe(true);
});

test("ON ONLY UNDER ITS PARENT: favourites on with the preview card off reads off", async () => {
  answer = async () => ({ task_favourites: { enabled: true }, canvas_rail_peek: { enabled: false } });
  const seen = await probe();
  expect(seen.every((v) => v === false)).toBe(true);
  // The parent switched on in this tab's Settings: on at once.
  await act(async () => {
    publishFavouritesParent({ enabled: true });
  });
  expect(seen[seen.length - 1]).toBe(true);
  // …and off again with it, favourites still on.
  await act(async () => {
    publishFavouritesParent({ enabled: false });
  });
  expect(seen[seen.length - 1]).toBe(false);
});

test("ANOTHER TAB'S PUT lands here live: off, then back on, no reload", async () => {
  answer = async () => BOTH_ON;
  const seen = await probe();
  expect(seen[seen.length - 1]).toBe(true);
  // The other tab, as a second channel on the same name.
  const otherTab = new BroadcastChannel("fused-render:prefs");
  try {
    otherTab.postMessage({ prefs: { task_favourites: { enabled: false } } });
    await settlesTo(seen, false);
    otherTab.postMessage({ prefs: { task_favourites: { enabled: true } } });
    await settlesTo(seen, true);
    // The PARENT flipped elsewhere takes the favourites with it.
    otherTab.postMessage({ prefs: { canvas_rail_peek: { enabled: false } } });
    await settlesTo(seen, false);
    otherTab.postMessage({ prefs: { canvas_rail_peek: { enabled: true } } });
    await settlesTo(seen, true);
  } finally {
    otherTab.close();
  }
});

test("the prefs PUT helper is what announces: its answer reaches every listener", async () => {
  answer = async () => BOTH_ON;
  const seen = await probe();
  answer = async () => ({ ...BOTH_ON, task_favourites: { enabled: false } });
  await act(async () => {
    await putTaskFavouritesEnabled(false);
  });
  await settlesTo(seen, false);
});

test("an OLDER revision heard after a newer one is ignored (late PUT answer)", async () => {
  answer = async () => BOTH_ON;
  const seen = await probe();
  const otherTab = new BroadcastChannel("fused-render:prefs");
  try {
    otherTab.postMessage({ prefs: { revision: 20, task_favourites: { enabled: false } } });
    await settlesTo(seen, false);
    // Answered first, heard last: the switch stays as the newer write left it.
    otherTab.postMessage({ prefs: { revision: 10, task_favourites: { enabled: true } } });
    await act(async () => {
      await new Promise((res) => setTimeout(res, 50));
    });
    expect(seen[seen.length - 1]).toBe(false);
    // A newer one still lands.
    otherTab.postMessage({ prefs: { revision: 30, task_favourites: { enabled: true } } });
    await settlesTo(seen, true);
  } finally {
    otherTab.close();
  }
});

test("a posted payload WITHOUT the field is not an answer about it", async () => {
  answer = async () => BOTH_ON;
  const seen = await probe();
  const otherTab = new BroadcastChannel("fused-render:prefs");
  try {
    otherTab.postMessage({ prefs: { canvases: { enabled: false } } });
    // A marker message after it: once THAT lands, the first one has too.
    otherTab.postMessage({ prefs: { task_favourites: { enabled: true } } });
    await act(async () => {
      await new Promise((res) => setTimeout(res, 50));
    });
    expect(seen.slice(seen.indexOf(true)).every((v) => v === true)).toBe(true);
  } finally {
    otherTab.close();
  }
});

test("a BroadcastChannel that refuses to open arms nothing and throws nothing", async () => {
  resetPrefsChannelForTests();
  const Real = globalThis.BroadcastChannel;
  const heard: unknown[] = [];
  let off!: () => void;
  (globalThis as { BroadcastChannel: unknown }).BroadcastChannel = function Refused() {
    throw new Error("SecurityError: refused");
  };
  try {
    expect(() => {
      off = onPrefsWrittenElsewhere((p) => heard.push(p));
    }).not.toThrow();
  } finally {
    (globalThis as { BroadcastChannel: unknown }).BroadcastChannel = Real;
  }
  // Nothing listens: another tab's post reaches no handler (the focus
  // re-read is the fallback), and the unsubscribe still works.
  const otherTab = new BroadcastChannel("fused-render:prefs");
  try {
    otherTab.postMessage({ prefs: { task_favourites: { enabled: false } } });
    await new Promise((res) => setTimeout(res, 50));
    expect(heard).toEqual([]);
  } finally {
    otherTab.close();
    off();
  }
});
