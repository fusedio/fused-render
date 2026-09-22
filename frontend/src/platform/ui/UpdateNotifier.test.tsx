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
const { getRetainedNotifications, getPopupNotification, _resetNotificationsForTest } = await import(
  "@platform/lib/notifications"
);

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
