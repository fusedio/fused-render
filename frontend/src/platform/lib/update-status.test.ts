// Pure logic only — `updateRelevant` and `updateLabel` are what the badge,
// the collapsed rail's dot, and the Settings popover row each gate/word
// themselves on, so a bug here would be wrong in three places at once.
import { afterEach, describe, expect, it } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { UpdateStatus } from "@platform/lib/api";
import { setEventsClientForTests } from "@platform/lib/events";
import {
  CHECK_RESULT_HOLD_MS,
  checkNowLabel,
  pokeUpdateStatus,
  resetUpdateStatusForTests,
  setUpdateStatus,
  shouldCheckOnReturn,
  updateLabel,
  updateRelevant,
  useUpdateStatus,
} from "./update-status";

function status(overrides: Partial<UpdateStatus>): UpdateStatus {
  return {
    state: "idle",
    method: "dmg",
    latest_version: null,
    progress: null,
    progress_total: null,
    error: null,
    manual_command: null,
    ...overrides,
  };
}

describe("updateRelevant", () => {
  it("is false for null and for idle/checking", () => {
    expect(updateRelevant(null)).toBe(false);
    expect(updateRelevant(status({ state: "idle" }))).toBe(false);
    expect(updateRelevant(status({ state: "checking" }))).toBe(false);
  });

  it("is true for available, installing, installed, and error", () => {
    for (const state of ["available", "installing", "installed", "error"]) {
      expect(updateRelevant(status({ state }))).toBe(true);
    }
  });
});

describe("updateLabel", () => {
  it("never puts the version in the label (it truncated; the version lives in the tooltip)", () => {
    expect(updateLabel(status({ state: "available", latest_version: "0.5.10" }))).toBe(
      "Update available"
    );
  });

  it("falls back to the bare phrase when no version is known", () => {
    expect(updateLabel(status({ state: "available", latest_version: null }))).toBe(
      "Update available"
    );
  });

  it("says Updating… while installing, regardless of version", () => {
    expect(
      updateLabel(status({ state: "installing", latest_version: "0.5.10" }))
    ).toBe("Updating…");
  });

  it("says Ready to restart once installed", () => {
    expect(updateLabel(status({ state: "installed", latest_version: "0.5.10" }))).toBe(
      "Ready to restart"
    );
  });

  it("falls to the available phrasing for error", () => {
    expect(updateLabel(status({ state: "error", latest_version: "0.5.10" }))).toBe(
      "Update available"
    );
  });
});


// The store itself: one subscription to the `update` topic however many
// surfaces read it, fed by the fake client (`setEventsClientForTests`) — the
// real one has no socket in bun and never calls back.
describe("useUpdateStatus", () => {
  let renderers: ReactTestRenderer[] = [];

  afterEach(() => {
    act(() => renderers.forEach((r) => r.unmount()));
    renderers = [];
    setEventsClientForTests(null);
    resetUpdateStatusForTests();
  });

  function fakeClient() {
    const state = { subscribes: 0, unsubscribes: 0, resyncs: 0, topics: [] as string[] };
    let push: ((snap: unknown) => void) | null = null;
    setEventsClientForTests({
      subscribe: ((topic: string, _p: unknown, cb: (s: unknown, d: unknown, m: Record<string, unknown>) => void) => {
        state.subscribes += 1;
        state.topics.push(topic);
        push = (snap) => cb(snap, null, { gen: null });
        return () => {
          state.unsubscribes += 1;
          push = null;
        };
      }) as never,
      resync: () => {
        state.resyncs += 1;
        return true;
      },
    });
    return { state, push: (snap: unknown) => push?.(snap) };
  }

  function Reader({ seen }: { seen: (s: UpdateStatus | null) => void }) {
    seen(useUpdateStatus());
    return null;
  }

  function mount(seen: (s: UpdateStatus | null) => void): ReactTestRenderer {
    let r!: ReactTestRenderer;
    act(() => {
      r = create(createElement(Reader, { seen }));
    });
    renderers.push(r);
    return r;
  }

  it("three readers share ONE subscription to `update`, closed with the last reader", () => {
    const { state, push } = fakeClient();
    const seen: (UpdateStatus | null)[] = [];
    mount((s) => seen.push(s));
    mount((s) => seen.push(s));
    mount((s) => seen.push(s));
    expect(state.subscribes).toBe(1);
    expect(state.topics).toEqual(["update"]);
    expect(seen.every((s) => s === null)).toBe(true);

    // The snapshot is `{ update }` — GET /api/config's `update` field — and
    // every reader sees it.
    act(() => push({ update: status({ state: "available", latest_version: "0.5.10" }) }));
    const last = seen.slice(-3);
    expect(last.every((s) => s?.state === "available" && s.latest_version === "0.5.10")).toBe(true);

    // No updater at all (a dev run): the field is null, and so is the store.
    act(() => push({ update: null }));
    expect(seen[seen.length - 1]).toBe(null);

    act(() => renderers.forEach((r) => r.unmount()));
    renderers = [];
    expect(state.unsubscribes).toBe(1);
  });

  it("a frame carrying the same status by value wakes nobody", () => {
    const { push } = fakeClient();
    let renders = 0;
    mount(() => {
      renders += 1;
    });
    act(() => push({ update: status({ state: "idle" }) }));
    const after = renders;
    act(() => push({ update: status({ state: "idle" }) }));
    expect(renders).toBe(after);
  });

  it("setUpdateStatus sets locally and pokeUpdateStatus is one resync, never a fetch", () => {
    const { state } = fakeClient();
    const seen = { latest: null as UpdateStatus | null };
    mount((s) => {
      seen.latest = s;
    });
    act(() => setUpdateStatus(status({ state: "installing" })));
    expect(seen.latest?.state).toBe("installing");
    pokeUpdateStatus();
    expect(state.resyncs).toBe(1);
    expect(state.subscribes).toBe(1);
  });
});

// Check-on-return: the app coming back to the front is what closes the gap
// between a release and the badge, so the three ways this could be wrong get
// pinned here — too eager, a dev run with no updater, and a `focus` that fired
// on a document nobody is looking at.
describe("shouldCheckOnReturn", () => {
  const GAP = 30 * 60_000;
  const idle = { state: "idle" } as UpdateStatus;

  it("checks once the gap has passed", () => {
    expect(shouldCheckOnReturn(0, GAP, idle, true)).toBe(true);
    expect(shouldCheckOnReturn(0, GAP + 1, idle, true)).toBe(true);
  });

  it("stays quiet inside the gap, so cmd-tabbing is not a run of requests", () => {
    expect(shouldCheckOnReturn(0, 0, idle, true)).toBe(false);
    expect(shouldCheckOnReturn(0, GAP - 1, idle, true)).toBe(false);
  });

  it("never checks without an updater — a dev run has none and the POST 404s", () => {
    expect(shouldCheckOnReturn(0, GAP * 10, null, true)).toBe(false);
  });

  it("never checks for a hidden document, however long it has been", () => {
    expect(shouldCheckOnReturn(0, GAP * 10, idle, false)).toBe(false);
  });

});

describe("shouldCheckOnReturn only re-asks from idle", () => {
  it("is false once an update is available, running, installed or failed", () => {
    for (const state of ["available", "installing", "installed", "error", "checking"] as const) {
      expect(shouldCheckOnReturn(0, 3_600_000, status({ state }), true)).toBe(false);
    }
    expect(shouldCheckOnReturn(0, 3_600_000, status({ state: "idle" }), true)).toBe(true);
  });
});

describe("checkNowLabel", () => {
  // The idle row's four phases, worded once here rather than read off a tree.
  it("offers the check at rest and says so while it runs", () => {
    expect(checkNowLabel("rest", "0.5.22")).toBe("Check for updates");
    expect(checkNowLabel("checking", "0.5.22")).toBe("Checking…");
  });

  it("names the version it is current at, when it knows it", () => {
    expect(checkNowLabel("current", "0.5.22")).toBe("Up to date · v0.5.22");
    // /api/config has not answered yet, or an old server without `version`.
    expect(checkNowLabel("current", null)).toBe("Up to date");
    expect(checkNowLabel("current", undefined)).toBe("Up to date");
  });

  it("owns up to a failed check without blaming anything", () => {
    expect(checkNowLabel("failed", "0.5.22")).toBe("Couldn't check");
  });

  it("holds an answer long enough to read, not long enough to look stuck", () => {
    expect(CHECK_RESULT_HOLD_MS).toBeGreaterThanOrEqual(3_000);
    expect(CHECK_RESULT_HOLD_MS).toBeLessThanOrEqual(6_000);
  });
});
