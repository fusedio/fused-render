// useScheduleEvents' §5 wiring (SPEC-quiet-notifications.md): narrator-only
// polling, and the info/error split (`started`/`done` suppressible+
// non-retained, `failed`/`missed` never suppressed+always retained).
//
// Drives the real hook via react-test-renderer (drafts.test.ts's own
// pattern) rather than mocking @platform/lib/api or @platform/lib/
// notifications — both are transitively imported everywhere, and
// appdoctor-lib.test.ts's header explains why `mock.module` on a module this
// widely shared is the wrong tool (process-wide, contaminates unrelated
// suites). `fetch` is stubbed directly (drafts.test.ts's `recordFetch`), and
// presence is driven through a real in-memory `localStorage` — the same
// surface a real browser gives every one of these modules, so no module
// needs an env-injection seam it doesn't already have.
import { beforeEach, describe, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

// A minimal in-memory localStorage — installed BEFORE the first import of
// presence.ts (transitively, via scheduleEvents.ts below), since presence's
// heartbeat mints this document's own entry at module-load time.
const presenceStore = new Map<string, string>();
(globalThis as unknown as { localStorage: Storage }).localStorage = {
  getItem: (k: string) => (presenceStore.has(k) ? (presenceStore.get(k) as string) : null),
  setItem: (k: string, v: string) => {
    presenceStore.set(k, v);
  },
  removeItem: (k: string) => {
    presenceStore.delete(k);
  },
  clear: () => presenceStore.clear(),
  key: () => null,
  length: 0,
} as Storage;

const { useScheduleEvents } = await import("@platform/lib/scheduleEvents");
const { _resetNotificationsForTest, getPopupNotification, getRetainedNotifications } =
  await import("@platform/lib/notifications");

const PRESENCE_KEY = "fused-render:presence";

/** Plants a top-level entry that sorts BEFORE this document's own minted
 *  windowId (which always starts with "w") — making isNarrator() false for
 *  this document, the same as a second, longer-lived tab already narrating. */
function plantForeignNarrator(): void {
  const raw = presenceStore.get(PRESENCE_KEY);
  const map = raw ? JSON.parse(raw) : {};
  map["a-foreign"] = { page: "", focused: true, ts: Date.now(), topLevel: true };
  presenceStore.set(PRESENCE_KEY, JSON.stringify(map));
}

function recordFetch(events: unknown[]): {
  calls: { url: string; init?: RequestInit }[];
  restore: () => void;
} {
  const calls: { url: string; init?: RequestInit }[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    if (String(url).includes("/api/schedule/events/ack")) {
      return { ok: true, json: async () => ({ delivered: 0 }) } as Response;
    }
    return { ok: true, json: async () => ({ events }) } as Response;
  }) as typeof fetch;
  return {
    calls,
    restore: () => {
      globalThis.fetch = real;
    },
  };
}

function mountHook(onOutcome?: () => void): { unmount: () => void } {
  let renderer!: ReactTestRenderer;
  const Probe = (): null => {
    useScheduleEvents(onOutcome);
    return null;
  };
  act(() => {
    renderer = create(createElement(Probe));
  });
  return {
    unmount: () => {
      act(() => {
        renderer.unmount();
      });
    },
  };
}

async function flush(): Promise<void> {
  // Two microtask hops: one for `getScheduleEvents()`'s awaited fetch+json,
  // one for the ack that follows it.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  presenceStore.clear();
  _resetNotificationsForTest();
});

describe("useScheduleEvents narrator gating", () => {
  test("a non-narrator window never polls or acks", async () => {
    plantForeignNarrator();
    const f = recordFetch([]);
    const h = mountHook();
    await flush();
    expect(f.calls.length).toBe(0);
    h.unmount();
    f.restore();
  });

  test("the narrator narrates a started event whose target isn't open here, then acks it", async () => {
    // SPEC-quiet-notifications.md §5's named trap, pinned at the wiring
    // layer: acking (which makes an unattended run's notice unrecoverable)
    // only ever happens AFTER `push()` — never before, and never skipped
    // just because the event went un-suppressed.
    const f = recordFetch([
      { id: 7, kind: "started", entry_id: "e1", target: "/somewhere/else",
        message: "do the thing", detail: "", ts: 0 },
    ]);
    const h = mountHook();
    await flush();

    const popup = getPopupNotification();
    expect(popup?.title).toContain("started");
    expect(popup?.tone).toBe("info");
    expect(popup?.action).toBeUndefined(); // §5: no action for started/done
    expect(getRetainedNotifications()).toEqual([]); // never retained

    const ack = f.calls.find((c) => c.url.includes("/api/schedule/events/ack"));
    expect(ack).toBeDefined();
    expect(JSON.parse(String(ack!.init!.body))).toEqual({ id: 7 });

    h.unmount();
    f.restore();
  });

  test("a done event is narrated the same suppressible, non-retained way", async () => {
    const f = recordFetch([
      { id: 8, kind: "done", entry_id: "e1", target: "/somewhere/else",
        message: "do the thing", detail: "", ts: 0 },
    ]);
    const h = mountHook();
    await flush();

    const popup = getPopupNotification();
    expect(popup?.title).toContain("finished");
    expect(popup?.tone).toBe("info");
    expect(getRetainedNotifications()).toEqual([]);

    h.unmount();
    f.restore();
  });

  test("a failed event is always retained with an Open action, never suppressed", async () => {
    const f = recordFetch([
      { id: 9, kind: "failed", entry_id: "e2", target: "/", message: "deploy",
        detail: "boom", ts: 0 },
    ]);
    const h = mountHook();
    await flush();

    const retained = getRetainedNotifications();
    expect(retained.length).toBe(1);
    expect(retained[0].tone).toBe("error");
    expect(retained[0].action).toBeDefined();

    h.unmount();
    f.restore();
  });

  test("onOutcome fires for started/done/failed but not for missed", async () => {
    let calls = 0;
    let f = recordFetch([
      { id: 1, kind: "started", entry_id: "e1", target: "/x", message: "m", detail: "", ts: 0 },
    ]);
    let h = mountHook(() => {
      calls += 1;
    });
    await flush();
    expect(calls).toBe(1);
    h.unmount();
    f.restore();

    f = recordFetch([
      { id: 2, kind: "missed", entry_id: "e3", target: "/x", message: "m", detail: "", ts: 0 },
    ]);
    h = mountHook(() => {
      calls += 1;
    });
    await flush();
    expect(calls).toBe(1); // unchanged
    h.unmount();
    f.restore();
  });
});
