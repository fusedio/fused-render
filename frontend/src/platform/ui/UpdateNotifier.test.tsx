// SPEC-update-notifications.md — the two decision notifications, driven by
// `UpdateNotifier` (headless: it renders `null`, so what these tests check is
// the notification STORE's own state after mounting it, not any DOM it
// produces).
//
// `updateInstall`/`updateCheck` go through `api.ts`'s `postJson`, which calls
// the real `fetch` — stubbed here directly (not via `mock.module`, which
// replaces a module for the whole bun PROCESS and would contaminate every
// other suite this file happens to share a `bun test` invocation with —
// `DownloadManager.test.tsx`'s own header comment made the same call for the
// same reason).
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

// A real Storage-shaped object for `sessionStorage` — bun's test runtime has
// no DOM, so (like `side-store.test.ts`'s own `localStorage`) nothing answers
// this global unless a suite that needs it supplies one first.
const sessionCells = new Map<string, string>();
Object.defineProperty(globalThis, "sessionStorage", {
  configurable: true,
  writable: true,
  value: {
    getItem: (k: string) => (sessionCells.has(k) ? (sessionCells.get(k) as string) : null),
    setItem: (k: string, v: string) => void sessionCells.set(k, String(v)),
    removeItem: (k: string) => void sessionCells.delete(k),
    clear: () => sessionCells.clear(),
    key: (i: number) => [...sessionCells.keys()][i] ?? null,
    get length() {
      return sessionCells.size;
    },
  } as Storage,
});

const { default: UpdateNotifier } = await import("@platform/ui/UpdateNotifier");
const { setUpdateStatus, resetUpdateStatusForTests } = await import("@platform/lib/update-status");
const { requestRestart, resetRestartForTests, noteRestartProbe } = await import(
  "@platform/lib/restart-store"
);
const {
  dismissNotification,
  notify,
  getRetainedNotifications,
  getPopupNotification,
  _resetNotificationsForTest,
} = await import("@platform/lib/notifications");

const realFetch = globalThis.fetch;
let installCalls: Array<string | null | undefined> = [];
const stubFetch = (async (url: unknown, init?: { method?: string; body?: string }) => {
  if (String(url) === "/api/update/install") {
    installCalls.push(JSON.parse(init?.body ?? "{}").expected_version);
    return {
      ok: true,
      json: async () => ({
        state: "installing",
        method: "dmg",
        latest_version: "0.5.81",
        progress: 0,
        progress_total: null,
        error: null,
        manual_command: null,
      }),
    };
  }
  return { ok: false, json: async () => ({}) };
}) as unknown as typeof fetch;

beforeEach(() => {
  globalThis.fetch = stubFetch;
  installCalls = [];
  sessionCells.clear();
  // BELT AND SUSPENDERS on top of the `afterEach` resets below (finding #8,
  // code review — a CI-only flake in the very first test in this file: green
  // 7138/0 on this machine, red 7137/1 on the Linux runner, same file and
  // test counts both times). The real mechanism was traced to
  // `update-status.ts`'s `poll()`: its staleness guard used to run AFTER the
  // mutating `set()` call rather than before it, so a stale `getConfig()`
  // fetch — started by an EARLIER test file's own component mount, which
  // reaches this same module-singleton poll loop — could resolve arbitrarily
  // later and silently overwrite the shared `current` state out from under a
  // completely different, later-running test file's `useUpdateStatus()`
  // subscriber (this one). `resetUpdateStatusForTests()` in `afterEach` bumps
  // the generation counter and clears the pending timer, but cannot cancel an
  // ALREADY in-flight fetch promise — so the race's odds depend on bun's
  // process-wide module registry and exact test scheduling, which is exactly
  // why it was timing/order-dependent rather than reliably reproducible
  // locally. `poll()` itself is now fixed to check staleness before `set()`
  // runs at all (the actual fix); resetting here too, before this file's own
  // very first status write, removes any window for a same-run leftover
  // (a stale in-flight poll from a test file that ran directly before this
  // one, before this file's own tests have made any status calls yet) to be
  // mistaken for real data by this file's assertions.
  resetUpdateStatusForTests();
  resetRestartForTests();
  _resetNotificationsForTest();
});

afterEach(() => {
  globalThis.fetch = realFetch;
  resetUpdateStatusForTests();
  resetRestartForTests();
  _resetNotificationsForTest();
});

type Status = {
  state: string;
  method: string;
  latest_version: string | null;
  progress: number | null;
  progress_total: number | null;
  error: string | null;
  manual_command: string | null;
  check_only?: boolean;
};

function status(overrides: Partial<Status>): Status {
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

async function mount(): Promise<ReactTestRenderer> {
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(<UpdateNotifier />);
  });
  return r;
}

test("raises the Download notification on `available`, nothing on `check_only`", async () => {
  const r = await mount();
  await act(async () => {
    setUpdateStatus(status({ state: "available", latest_version: "0.5.81" }));
  });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(1);
  expect(retained[0].title).toContain("Update available");
  expect(retained[0].title).toContain("0.5.81");

  _resetNotificationsForTest();
  await act(async () => {
    setUpdateStatus(status({ state: "available", latest_version: "0.5.81", check_only: true }));
  });
  expect(getRetainedNotifications()).toHaveLength(0);
  await act(async () => r.unmount());
});

test("pressing Download calls updateInstall with the version on screen, then dismisses", async () => {
  const r = await mount();
  await act(async () => {
    setUpdateStatus(status({ state: "available", latest_version: "0.5.81" }));
  });
  const card = getRetainedNotifications()[0];
  await act(async () => {
    card.action?.onClick();
  });
  // The dismiss is synchronous (before the install's own await resolves);
  // give the install's microtasks a turn before asserting on it too.
  expect(getRetainedNotifications()).toHaveLength(0);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(installCalls).toEqual(["0.5.81"]);
  await act(async () => r.unmount());
});

test("raises the Restart notification on `installed`", async () => {
  const r = await mount();
  await act(async () => {
    setUpdateStatus(status({ state: "installed", latest_version: "0.5.81" }));
  });
  const retained = getRetainedNotifications();
  expect(retained).toHaveLength(1);
  expect(retained[0].title).toBe("Update ready");
  expect(retained[0].action?.label).toBe("Restart now");
  await act(async () => r.unmount());
});

test("dismissing (Later) records the version and suppresses re-raise for it, but not for a later version", async () => {
  const r = await mount();
  await act(async () => {
    setUpdateStatus(status({ state: "installed", latest_version: "0.5.81" }));
  });
  const card = getRetainedNotifications()[0];
  await act(async () => {
    card.extraAction?.onClick();
  });
  expect(getRetainedNotifications()).toHaveLength(0);

  // Same version re-poked (e.g. the poll landing again with nothing new) does
  // not bring it back.
  await act(async () => {
    setUpdateStatus(status({ state: "installed", latest_version: "0.5.81" }));
  });
  expect(getRetainedNotifications()).toHaveLength(0);

  // A NEWER version is a different decision — it re-raises.
  await act(async () => {
    setUpdateStatus(status({ state: "installed", latest_version: "0.5.82" }));
  });
  expect(getRetainedNotifications()).toHaveLength(1);
  expect(getRetainedNotifications()[0].title).toBe("Update ready");
  await act(async () => r.unmount());
});

test("dismissing the restart card directly (the panel's own ✕) does not resurrect it (finding #3)", async () => {
  // `RepoUpdatesDock`'s ✕ and "Dismiss all" call `dismissNotification(id)`
  // straight into the store — they bypass this component's own "Later"
  // handler entirely, so they never call `recordRestartDismissed`. Before
  // finding #3's fix, the raise effect could not tell that apart from a
  // `capRetained` eviction (both look identical: "our id vanished from
  // `retained`") and treated it as safe to resurrect, popping the exact card
  // the user had just closed straight back on the very next status update.
  const r = await mount();
  await act(async () => {
    setUpdateStatus(status({ state: "installed", latest_version: "0.5.81" }));
  });
  const card = getRetainedNotifications()[0];
  await act(async () => {
    dismissNotification(card.id);
  });
  expect(getRetainedNotifications()).toHaveLength(0);

  // The poll landing again with nothing new (the ordinary steady-state case)
  // must not bring it back.
  await act(async () => {
    setUpdateStatus(status({ state: "installed", latest_version: "0.5.81" }));
  });
  expect(getRetainedNotifications()).toHaveLength(0);
  await act(async () => r.unmount());
});

test("a genuine cap eviction still resurrects the restart card (finding #3)", async () => {
  // The other half of finding #3: the fix must not turn a REAL eviction into
  // a silent, permanent loss either. Filling the retained list past
  // `MAX_RETAINED` pushes the restart card (the oldest row) out the same way
  // a busy notification stream would. Note this resurrects WITHIN the same
  // `act()` that causes the eviction, not on a later poke: the eviction
  // itself changes `retained`, which is one of this effect's own
  // dependencies, so React reruns it immediately — same-render eviction and
  // recovery, exactly as it did before this fix (only the DISMISSAL path,
  // tested above, now behaves differently).
  const r = await mount();
  await act(async () => {
    setUpdateStatus(status({ state: "installed", latest_version: "0.5.81" }));
  });
  const beforeId = getRetainedNotifications()[0].id;
  expect(getRetainedNotifications().some((n) => n.title === "Update ready")).toBe(true);

  await act(async () => {
    for (let i = 0; i < 5; i++) {
      notify({ title: `Other notice ${i}`, tier: "attention", action: { label: "x", onClick: () => {} } });
    }
  });
  const restartCard = getRetainedNotifications().find((n) => n.title === "Update ready");
  expect(restartCard).toBeDefined();
  // Resurrected as a FRESH row (a new id) — its old slot is the one that got
  // sliced off by `capRetained`, so it comes back at the end like any other
  // freshly-`notify()`'d card, not back in its original position.
  expect(restartCard?.id).not.toBe(beforeId);
  await act(async () => r.unmount());
});

test("Later works when the server reports no version string (finding #4)", async () => {
  // `latest_version` can legitimately be `null` (`state: "installed"` with no
  // version attached). The old code's bare `if (version) recordRestartDismissed(version)`
  // silently skipped recording anything for that case — "Later" dismissed the
  // popup, but with nothing recorded, `wasRestartDismissed` never matched and
  // the very next render (retained changed → effect re-ran) put the card
  // straight back, making "Later" a no-op whenever the version was null.
  const r = await mount();
  await act(async () => {
    setUpdateStatus(status({ state: "installed", latest_version: null }));
  });
  const card = getRetainedNotifications()[0];
  expect(card.title).toBe("Update ready");
  await act(async () => {
    card.extraAction?.onClick();
  });
  expect(getRetainedNotifications()).toHaveLength(0);

  // Re-poked with the same (null) version — must stay dismissed.
  await act(async () => {
    setUpdateStatus(status({ state: "installed", latest_version: null }));
  });
  expect(getRetainedNotifications()).toHaveLength(0);
  await act(async () => r.unmount());
});

test("the restart card loses its ✕ and re-notifies in place while restartInFlight", async () => {
  const r = await mount();
  await act(async () => {
    setUpdateStatus(status({ state: "installed", latest_version: "0.5.81" }));
  });
  const beforeId = getRetainedNotifications()[0].id;
  expect(getRetainedNotifications()[0].dismissible).toBe(true);

  await act(async () => {
    requestRestart();
  });
  // Same id — the in-flight narration REPLACES the ready card, it does not
  // stack a second one (spec: "the same card narrates it, in place").
  const popup = getPopupNotification();
  expect(popup?.id).toBe(beforeId);
  expect(popup?.dismissible).toBe(false);
  expect(popup?.title).toBe("Restarting fused-render");

  // A probe failing twice moves the stage to "reconnecting" — still in
  // flight, still no ✕, same id, new stage copy.
  await act(async () => {
    noteRestartProbe({ ok: false });
    noteRestartProbe({ ok: false });
  });
  const reconnecting = getPopupNotification();
  expect(reconnecting?.id).toBe(beforeId);
  expect(reconnecting?.dismissible).toBe(false);
  expect(reconnecting?.detail).toBe("Reconnecting…");

  await act(async () => r.unmount());
});
