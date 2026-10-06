// UpdateCard — one title/buttons/click behaviour per update state. Stores are
// driven through their own set/reset helpers; fetch is stubbed directly (no
// mock.module, which is process-wide in bun).
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { default: UpdateCard, updateCardActive } = await import("@shell/UpdateCard");
const { setUpdateStatus, resetUpdateStatusForTests } = await import("@platform/lib/update-status");
const { resetRestartForTests, requestRestart } = await import("@platform/lib/restart-store");

const realFetch = globalThis.fetch;
let installCalls = 0;
const stubFetch = (async (url: unknown) => {
  if (String(url) === "/api/update/install") installCalls += 1;
  return { ok: true, json: async () => ({ state: "installing", method: "dmg", latest_version: "9.9.9" }) };
}) as unknown as typeof fetch;

const base = {
  method: "dmg",
  latest_version: "9.9.9",
  progress: null,
  progress_total: null,
  error: null,
  manual_command: null,
};
let r: ReactTestRenderer | null = null;

const clearSession = () => {
  try {
    sessionStorage.clear();
  } catch {
    /* no sessionStorage in this shim */
  }
};

beforeEach(() => {
  installCalls = 0;
  globalThis.fetch = stubFetch;
  resetUpdateStatusForTests();
  resetRestartForTests();
  clearSession();
});
afterEach(() => {
  act(() => r?.unmount());
  r = null;
  globalThis.fetch = realFetch;
  resetUpdateStatusForTests();
  resetRestartForTests();
  clearSession();
});

function mount(status: Record<string, unknown> | null) {
  act(() => {
    setUpdateStatus(status as never);
  });
  act(() => {
    r = create(<UpdateCard />);
  });
  return r!;
}
const buttons = (t: ReactTestRenderer) => t.root.findAllByType("button");
const label = (t: ReactTestRenderer, s: string) => buttons(t).find((b) => b.findAll((n) => n.children.includes(s)).length > 0);
const text = (t: ReactTestRenderer) => JSON.stringify(t.toJSON());

test("renders nothing for idle and check_only", () => {
  expect(mount({ ...base, state: "idle" }).toJSON()).toBeNull();
  act(() => r?.unmount());
  expect(mount({ ...base, state: "available", check_only: true }).toJSON()).toBeNull();
});

test("available: Download button installs", async () => {
  const t = mount({ ...base, state: "available", check_only: false });
  expect(text(t)).toContain("Update available");
  expect(text(t)).toContain("v9.9.9 is ready to download.");
  await act(async () => {
    label(t, "Download")!.props.onClick();
  });
  expect(installCalls).toBe(1);
});

test("installing: progress, no buttons", () => {
  const t = mount({ ...base, state: "installing" });
  expect(text(t)).toContain("Downloading update");
  expect(text(t)).toContain("Downloading v9.9.9");
  expect(buttons(t).length).toBe(0);
});

test("installed + ready: Restart now and Later", () => {
  const t = mount({ ...base, state: "installed" });
  expect(text(t)).toContain("Update ready");
  expect(text(t)).toContain("v9.9.9 is installed. Restart to start using it.");
  expect(label(t, "Later")).toBeDefined();
  expect(() => act(() => label(t, "Restart now")!.props.onClick())).not.toThrow();
});

test("installed + restart in flight: Restarting…, no buttons", () => {
  const t = mount({ ...base, state: "installed" });
  act(() => requestRestart());
  expect(text(t)).toContain("Restarting…");
  expect(buttons(t).length).toBe(0);
});

test("error: Try again reinstalls, shows the error text", async () => {
  const t = mount({ ...base, state: "error", error: "disk full" });
  expect(text(t)).toContain("Update failed");
  expect(text(t)).toContain("disk full");
  await act(async () => {
    label(t, "Try again")!.props.onClick();
  });
  expect(installCalls).toBe(1);
});

test("error without text falls back", () => {
  const t = mount({ ...base, state: "error" });
  expect(text(t)).toContain("Couldn't reach the update server.");
});

test("Later collapses to a compact row; clicking it restarts", () => {
  const t = mount({ ...base, state: "installed" });
  act(() => label(t, "Later")!.props.onClick());
  expect(text(t)).not.toContain("Update ready");
  expect(text(t)).toContain("Restart to update");
  expect(text(t)).toContain("v9.9.9");
  expect(label(t, "Later")).toBeUndefined();
  act(() => buttons(t)[0].props.onClick());
  expect(text(t)).toContain("Restarting…");
});

test("updateCardActive mirrors what the card shows", () => {
  expect(updateCardActive(null, "ready")).toBe(false);
  expect(updateCardActive({ ...base, state: "available", check_only: true } as never, "ready")).toBe(false);
  expect(updateCardActive({ ...base, state: "available", check_only: false } as never, "ready")).toBe(true);
  expect(updateCardActive({ ...base, state: "error" } as never, "ready")).toBe(true);
  expect(updateCardActive({ ...base, state: "installed" } as never, "ready")).toBe(true);
});
