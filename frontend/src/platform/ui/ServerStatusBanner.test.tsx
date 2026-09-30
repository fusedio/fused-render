// THE INSTALL LANDING WAKES THE PROBE (Akshil, 2026-09-19).
//
// Two clocks know the update is on disk, and they are not the same clock: the
// shared update store polls `/api/update/status` every 2 s while an install is
// running and reports `state: "installed"` the moment the swap ends, while
// this component's own `/api/config` probe — the one that reads
// `installed_version` — runs every `POLL_MS` (5 s). `bannerSurface` already
// suppresses the down card on either door, so what is pinned here is the
// SECOND fact: the moment the store says `installed`, the banner asks
// `/api/config` straight away instead of waiting out its own tick, so the
// restart NOTIFICATION (`UpdateNotifier`, driven off `installed_version`
// elsewhere) can name the version that is actually waiting rather than a
// fallback. This component itself draws nothing for the restart case any
// more (SPEC-update-notifications.md) — these tests assert the probe fires
// on the right cadence and that the banner stays silent throughout.
//
// No fake timers: the whole point is that nothing has to advance. The
// assertions run inside a few milliseconds of the transition, and `POLL_MS` is
// never reached in this file — a probe that only fired on the interval would
// leave the counter at zero.
import {
  installDomShim,
  installPortalContainer,
  removePortalContainer,
} from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestInstance } from "react-test-renderer";

const { default: ServerStatusBanner } = await import("@platform/ui/ServerStatusBanner");
const { resetUpdateStatusForTests, setUpdateStatus } = await import(
  "@platform/lib/update-status"
);
const { resetRestartForTests } = await import("@platform/lib/restart-store");

/** What `/api/config` answers, swapped mid-test the way the server's own answer
 *  changes the instant the installer has swapped the app on disk. */
let config: Record<string, unknown> = { version: "0.5.96", installed_version: null, dev: false };
/** Only the BANNER's probes. `cache: "no-store"` is the banner's own (the
 *  shared store's poll goes through `getConfig`, which sets no cache option),
 *  so the two pollers hitting the same URL can still be told apart. */
let probes = 0;

// INSTALLED PER TEST AND TAKEN AWAY AGAIN. `bun test` shares one `globalThis`
// across every file in a run, and this stub answers /api/config with a HEALTHY
// server — leave it behind and the next suite that mounts this banner
// (`NotificationHost.test.tsx`, whose own card runs for five seconds) gets a
// probe that succeeds, decides the served bundle moved, and portals a refresh
// dialog into a `document.body` that suite never installed.
const realFetch = globalThis.fetch;
const stubFetch = (async (url: unknown, init?: { cache?: string }) => {
  if (String(url).startsWith("/api/config")) {
    if (init?.cache === "no-store") probes += 1;
    return { ok: true, json: async () => config };
  }
  return { ok: false, json: async () => ({}) };
}) as unknown as typeof fetch;

beforeEach(() => {
  globalThis.fetch = stubFetch;
});

const mounted: Array<ReturnType<typeof create>> = [];
async function mount() {
  installPortalContainer();
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<ServerStatusBanner />);
  });
  mounted.push(r);
  return r;
}

afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  removePortalContainer();
  resetUpdateStatusForTests();
  resetRestartForTests();
  probes = 0;
  config = { version: "0.5.96", installed_version: null, dev: false };
  globalThis.fetch = realFetch;
});

function text(node: ReactTestInstance): string {
  let out = "";
  const walk = (children: unknown[]) => {
    for (const c of children) {
      if (typeof c === "string") out += c;
      else if (typeof c === "number") out += String(c);
      else if (c && typeof c === "object" && "children" in (c as ReactTestInstance))
        walk((c as ReactTestInstance).children as unknown[]);
    }
  };
  walk(node.children as unknown[]);
  return out;
}

test("the store turning 'installed' probes /api/config at once, without waiting out POLL_MS", async () => {
  const r = await mount();
  // Nothing has been asked yet: the banner's probe is armed on an interval,
  // and no interval has fired.
  expect(probes).toBe(0);

  // The install landed — the disk moved (what the next /api/config will say)
  // and the shared store heard about it first, on its own 2 s cadence.
  config = { version: "0.5.96", installed_version: "0.5.97", dev: false };
  await act(async () => {
    setUpdateStatus({ state: "installed", latest_version: null } as never);
  });

  expect(probes).toBe(1);
  // And the banner itself draws nothing — `installed` suppresses the down
  // card (step 5) but raises no dialog of its own any more; the restart
  // notification that names "0.5.97" lives in `UpdateNotifier`, not here.
  expect(text(r.root)).toBe("");
});

test("a re-render while the dialog sits there does not re-probe, and a later install does", async () => {
  await mount();
  config = { version: "0.5.96", installed_version: "0.5.97", dev: false };
  await act(async () => {
    setUpdateStatus({ state: "installed", latest_version: null } as never);
  });
  expect(probes).toBe(1);

  // Same state, a new object — every store push re-renders every subscriber,
  // and a probe per render would be a request every 2 s for as long as the
  // dialog is on screen.
  await act(async () => {
    setUpdateStatus({ state: "installed", latest_version: "0.5.97" } as never);
  });
  expect(probes).toBe(1);

  // Leaving `installed` re-arms it, so the NEXT install gets its own probe.
  await act(async () => {
    setUpdateStatus({ state: "idle", latest_version: null } as never);
  });
  await act(async () => {
    setUpdateStatus({ state: "installed", latest_version: null } as never);
  });
  expect(probes).toBe(2);
});
