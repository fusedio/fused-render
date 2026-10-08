// The Updates chip's wording per state (pure `updatesChip` / `updatesDetail`)
// and the popover's composition, rendered through `UpdatesCardView` with a
// fixed status — no poll, no network, no restart store.
import { expect, test } from "bun:test";
import { create, type ReactTestRendererJSON } from "react-test-renderer";

import type { UpdateStatus } from "@platform/lib/api";
import { downloadProgressLine, UpdatesCardView, updatesChip, updatesDetail } from "@shell/UpdatesDock";

const base: UpdateStatus = {
  state: "idle",
  method: "dmg",
  latest_version: "9.9.9",
  progress: null,
  progress_total: null,
  error: null,
  manual_command: null,
};
const st = (over: Partial<UpdateStatus>): UpdateStatus => ({ ...base, ...over });

test("at rest the chip is the running version; no updater reads the same", () => {
  expect(updatesChip(null, "ready", "0.6.22")).toEqual({ label: "v0.6.22", tone: "idle" });
  expect(updatesChip(st({ state: "idle" }), "ready", "0.6.22")).toEqual({ label: "v0.6.22", tone: "idle" });
  expect(updatesChip(null, "ready", null).label).toBe("Updates");
});

test("a manual press shows its own phases over an idle store", () => {
  expect(updatesChip(st({}), "ready", "1", "checking").label).toBe("Checking…");
  expect(updatesChip(st({}), "ready", "1", "current").label).toBe("Up to date");
  expect(updatesChip(st({}), "ready", "1", "failed").label).toBe("Couldn't check");
  expect(updatesChip(st({ state: "checking" }), "ready", "1").label).toBe("Checking…");
});

test("every update state has its own label, tone and progress", () => {
  expect(updatesChip(st({ state: "available" }), "ready", "1")).toEqual({ label: "Update available", tone: "on" });
  expect(updatesChip(st({ state: "installing", phase: "downloading", progress: 50, progress_total: 200 }), "ready", "1")).toEqual({
    label: "Downloading",
    tone: "on",
    progress: 0.25,
  });
  expect(updatesChip(st({ state: "installing", phase: "downloading", progress: 50, progress_total: null }), "ready", "1").progress).toBeNull();
  expect(updatesChip(st({ state: "installing", phase: "installing" }), "ready", "1")).toEqual({ label: "Installing", tone: "on", progress: null });
  expect(updatesChip(st({ state: "installed" }), "ready", "1")).toEqual({ label: "Restart to update", tone: "on" });
  expect(updatesChip(st({ state: "installed" }), "quitting", "1")).toEqual({ label: "Restarting…", tone: "on", progress: null });
  expect(updatesChip(st({ state: "error", error: "boom" }), "ready", "1")).toEqual({ label: "Update failed", tone: "failure" });
});

test("the detail sentence names the version and carries one action", () => {
  expect(updatesDetail(st({ state: "available" }), "ready")?.action?.label).toBe("Download");
  expect(updatesDetail(st({ state: "available", check_only: true }), "ready")?.action).toBeNull();
  expect(updatesDetail(st({ state: "installed" }), "ready")?.action?.label).toBe("Restart now");
  expect(updatesDetail(st({ state: "error", error: "no network" }), "ready")).toMatchObject({ detail: "no network", error: true });
  expect(updatesDetail(st({ state: "idle" }), "ready")).toBeNull();
  expect(downloadProgressLine(st({ state: "installing", phase: "downloading", progress: 12 * 1024 ** 2, progress_total: 180 * 1024 ** 2 }))).toBe("12 MB of 180 MB");
  expect(downloadProgressLine(st({ state: "installing", phase: "installing", progress: 5 }))).toBe("");
});

function texts(node: ReactTestRendererJSON | ReactTestRendererJSON[] | string | null): string[] {
  if (node === null) return [];
  if (typeof node === "string") return [node];
  if (Array.isArray(node)) return node.flatMap(texts);
  return (node.children ?? []).flatMap((c) => texts(c as ReactTestRendererJSON | string));
}

function view(status: UpdateStatus | null, over: Partial<Parameters<typeof UpdatesCardView>[0]> = {}) {
  return create(
    <UpdatesCardView
      status={status}
      stage="ready"
      version="0.6.22"
      phase="rest"
      onCheck={() => {}}
      autoDownload={true}
      onToggleAuto={() => {}}
      collapsed={false}
      onToggle={() => {}}
      {...over}
    />,
  );
}

test("open at rest: running version, the Check button and the auto-download row", () => {
  const all = texts(view(st({})).toJSON()).join(" | ");
  expect(all).toContain("Running v0.6.22");
  expect(all).toContain("Check for updates");
  expect(all).toContain("Automatically download updates");
});

test("no updater: the version still shows, the Check button does not", () => {
  const all = texts(view(null, { hasUpdater: false }).toJSON()).join(" | ");
  expect(all).toContain("Running v0.6.22");
  expect(all).not.toContain("Check for updates");
  expect(all).toContain("Updates aren’t managed from inside the app on this build.");
  expect(all).not.toContain("Automatically download updates");
});

test("updater present but store not heard yet: no 'not managed' flash, the Check button stands", () => {
  const all = texts(view(null, { hasUpdater: true }).toJSON()).join(" | ");
  expect(all).toContain("Check for updates");
  expect(all).not.toContain("managed from inside");
});

test("an update found replaces the Check button with the decision", () => {
  const r = view(st({ state: "available" }));
  const all = texts(r.toJSON()).join(" | ");
  expect(all).toContain("v9.9.9 is ready to download.");
  expect(all).toContain("Download");
  expect(all).not.toContain("Check for updates");
});

test("collapsed: the chip alone, no panel", () => {
  const all = texts(view(st({}), { collapsed: true }).toJSON()).join(" | ");
  expect(all).toContain("v0.6.22");
  expect(all).not.toContain("Running");
});
