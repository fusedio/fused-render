// UpdateChip — one label/click behaviour per update state. Stores are driven
// through their own set/reset helpers; fetch is stubbed directly (no
// mock.module, which is process-wide in bun).
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { default: UpdateChip } = await import("@shell/UpdateChip");
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

beforeEach(() => {
  installCalls = 0;
  globalThis.fetch = stubFetch;
  resetUpdateStatusForTests();
  resetRestartForTests();
});
afterEach(() => {
  act(() => r?.unmount());
  r = null;
  globalThis.fetch = realFetch;
  resetUpdateStatusForTests();
  resetRestartForTests();
});

function mount(status: Record<string, unknown> | null) {
  act(() => {
    setUpdateStatus(status as never);
  });
  act(() => {
    r = create(<UpdateChip />);
  });
  return r!;
}
const btn = (t: ReactTestRenderer) => t.root.findAllByType("button")[0];
const text = (t: ReactTestRenderer) => JSON.stringify(t.toJSON());
const evt = () => {
  const e = { stopped: false, stopPropagation() { e.stopped = true; } };
  return e;
};

test("renders nothing for idle, check_only, and error", () => {
  expect(mount({ ...base, state: "idle" }).toJSON()).toBeNull();
  act(() => r?.unmount());
  expect(mount({ ...base, state: "available", check_only: true }).toJSON()).toBeNull();
  act(() => r?.unmount());
  expect(mount({ ...base, state: "error" }).toJSON()).toBeNull();
});

test("available: Update available, click downloads and stops propagation", async () => {
  const t = mount({ ...base, state: "available", check_only: false });
  expect(text(t)).toContain("Update available");
  expect(btn(t).props.title).toBe("Download v9.9.9");
  const e = evt();
  await act(async () => {
    btn(t).props.onClick(e);
  });
  expect(e.stopped).toBe(true);
  expect(installCalls).toBe(1);
});

test("installing: muted non-clickable Updating…", () => {
  const t = mount({ ...base, state: "installing" });
  expect(text(t)).toContain("Updating…");
  expect(btn(t).props.disabled).toBe(true);
  expect(btn(t).props.onClick).toBeUndefined();
});

test("installed + ready: Restart to update, click requests restart", () => {
  const t = mount({ ...base, state: "installed" });
  expect(text(t)).toContain("Restart to update");
  expect(btn(t).props.title).toBe("v9.9.9 installed — restart to start using it");
  const e = evt();
  expect(() => btn(t).props.onClick(e)).not.toThrow();
  expect(e.stopped).toBe(true);
});

test("installed + restart in flight: non-clickable Restarting…", () => {
  const t = mount({ ...base, state: "installed" });
  act(() => requestRestart());
  expect(text(t)).toContain("Restarting…");
  expect(btn(t).props.disabled).toBe(true);
});
